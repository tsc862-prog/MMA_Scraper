#!/usr/bin/env python3
"""
pluto_scraper.py
Matches Bellator replays on Pluto TV (free, ad-supported, Paramount-owned) to
events and fights in Supabase. Two series are crawled (see SERIES):

  - "Bellator MMA Full Fight Cards"  → events.pluto_url
        136 full-event replays, Bellator 58–301 (with gaps) plus "Bellator NYC".
        Titles carry the event number ("Bellator 197: Chandler vs Girtz"), which
        is matched against the number in the Sherdog event name ("Bellator 197 -
        Chandler vs. Girtz", "BFC - Bellator Fighting Championships 58"). Never
        match on Pluto's air dates — they are TV air dates and can be months off
        (Bellator 131 is dated eight months after the event).
  - "Bellator MMA Individual Fights" → fights.pluto_url
        Single-bout VODs titled "A vs. B From Bellator N"; the Pluto *season*
        number is the event number, so each bout is resolved only against the
        fights on that card (surname match is safe when anchored to one card).

Data comes from Pluto's unauthenticated boot endpoint, which returns every
episode of a series (id, title, season/number) in one call — no token, no
paging, no HTML. Stored URLs use the id-only on-demand form, which Pluto
redirects to the canonical /shows/<n>/episode/<id>/ page:
  https://pluto.tv/us/on-demand/series/<seriesId>/season/<n>/episode/<episodeId>
Pluto is US-region; availability elsewhere is unverified.

Only changed URLs are written (events/fights carry audit triggers), so a
nightly run after the initial backfill is ~2 API calls and no DB writes.

  python pluto_scraper.py            # dry run, writes pluto_matches.txt only
  python pluto_scraper.py --push     # also update pluto_url columns
"""

import os
import re
import sys
import json
import uuid
import argparse
import unicodedata
import difflib
import requests

# Load .env
_env_path = os.path.join(os.path.dirname(__file__), '.env')
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith('#') and '=' in _line:
                _k, _v = _line.split('=', 1)
                os.environ.setdefault(_k.strip(), _v.strip())

SUPABASE_URL = os.environ.get('SUPABASE_URL', '').rstrip('/')
SUPABASE_KEY = os.environ.get('SUPABASE_KEY', '')

# Each entry is one Pluto series. 'kind' picks the matcher: 'event' matches
# whole-card replays to events, 'fight' matches single bouts to fights.
# 'org' is the events.organization value the series is scoped to, and
# 'num_re' pulls the event number out of that org's Sherdog event names.
# Adding another promotion Pluto carries as a series = one entry here.
SERIES = [
    {
        'slug':   'bellator-mma-full-fight-cards',
        'kind':   'event',
        'org':    'Bellator',
        'num_re': re.compile(r'\bBellator(?: Fighting Championships)?\s+(\d+)\b', re.I),
    },
    {
        'slug':   'bellator-mma-individual-fights',
        'kind':   'fight',
        'org':    'Bellator',
        'num_re': re.compile(r'\bBellator(?: Fighting Championships)?\s+(\d+)\b', re.I),
    },
]

BOOT_URL         = 'https://boot.pluto.tv/v4/start'
EPISODE_URL_TMPL = ('https://pluto.tv/us/on-demand/series/{series_id}'
                    '/season/{season}/episode/{episode_id}')
OUTPUT_FILE      = 'pluto_matches.txt'
UA               = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36')

sys.stdout.reconfigure(encoding='utf-8')

# ── Supabase helpers ──────────────────────────────────────────────────────────

def _sb_headers():
    return {
        'apikey': SUPABASE_KEY,
        'Authorization': f'Bearer {SUPABASE_KEY}',
        'Content-Type': 'application/json',
        'Prefer': 'return=minimal',
    }

def fetch_all(table, select='*', extra='', order='id'):
    # order=id: PostgREST offset paging without an ORDER BY is not stable —
    # rows can be skipped or repeated between pages, which silently drops
    # fights from the in-scope set.
    PAGE, offset, out = 1000, 0, []
    while True:
        url = (f'{SUPABASE_URL}/rest/v1/{table}'
               f'?select={select}&order={order}&limit={PAGE}&offset={offset}{extra}')
        r = requests.get(url, headers=_sb_headers())
        if not r.ok:
            print(f'  [WARN] fetch {table} failed ({r.status_code}): {r.text[:200]}')
            break
        batch = r.json()
        out.extend(batch)
        if len(batch) < PAGE:
            break
        offset += PAGE
    return out

def update_pluto_url(table, row_id, url):
    r = requests.patch(f'{SUPABASE_URL}/rest/v1/{table}?id=eq.{row_id}',
                       headers=_sb_headers(),
                       data=json.dumps({'pluto_url': url}))
    return r.status_code in (200, 204)

# ── Pluto TV ──────────────────────────────────────────────────────────────────

def fetch_series(slugs):
    """One boot call returns the full episode list for every requested series
    slug — {slug: series_dict}. Series Pluto doesn't know are simply absent."""
    params = {
        'appName': 'web', 'appVersion': '9.0.0',
        'deviceVersion': '128.0.0', 'deviceModel': 'web',
        'deviceMake': 'chrome', 'deviceType': 'web',
        'clientID': str(uuid.uuid4()), 'clientModelNumber': '1.0.0',
        'serverSideAds': 'false',
        'seriesIDs': ','.join(slugs),
    }
    r = requests.get(BOOT_URL, params=params, headers={'User-Agent': UA}, timeout=30)
    r.raise_for_status()
    return {s['slug']: s for s in r.json().get('VOD', []) if s.get('slug')}

def series_episodes(series):
    """Flatten seasons → [(season_number, episode)], in Pluto's order."""
    out = []
    for season in series.get('seasons', []):
        for ep in season.get('episodes', []):
            out.append((ep.get('season') or season.get('number'), ep))
    return out

def episode_url(series, season, ep):
    return EPISODE_URL_TMPL.format(series_id=series['id'], season=season,
                                   episode_id=ep['_id'])

# ── Name helpers ──────────────────────────────────────────────────────────────

def _fold(s):
    """Lowercase, accent-fold, collapse whitespace."""
    s = unicodedata.normalize('NFKD', s)
    s = ''.join(c for c in s if not unicodedata.combining(c))
    return re.sub(r'\s+', ' ', s).strip().lower()

def surname(name):
    """Last-name token, ignoring generational suffixes (Jr., II, …) so every
    '… Jr.' doesn't key under 'jr'. Used on both sides of a comparison."""
    toks = _fold(name).replace('.', '').split()
    while len(toks) > 1 and toks[-1] in {'jr', 'sr', 'ii', 'iii', 'iv'}:
        toks = toks[:-1]
    return toks[-1] if toks else ''

# Trailing rematch markers on a fighter name in a VOD title:
# "Douglas Lima I", "Patricio Pitbull 2", "Eddie Alvarez II".
_REMATCH_TAIL = re.compile(r'\s+(?:[IVX]+|\d+)$')

def _clean_name(s):
    s = re.sub(r'\([^)]*\)', '', s)          # parentheticals
    s = _REMATCH_TAIL.sub('', s.strip())
    return s.strip(' -–—.!?')

_FIGHT_TITLE_RE = re.compile(
    r'^(?P<a>.+?)\s+vs?\.?\s+(?P<b>.+?)\s+from\s+(?P<event>.+?)\s*$', re.I)

def parse_fight_title(title):
    """'Andrey Koreshkov vs. Douglas Lima I From Bellator 140'
       → ('Andrey Koreshkov', 'Douglas Lima', 'Bellator 140'); None if the
       title isn't in that shape (e.g. compilations)."""
    m = _FIGHT_TITLE_RE.match(title)
    if not m:
        return None
    a, b = _clean_name(m.group('a')), _clean_name(m.group('b'))
    if len(a) < 2 or len(b) < 2:
        return None
    return a, b, m.group('event').strip()

# ── Event matching ────────────────────────────────────────────────────────────

def build_event_index(events, num_re):
    """{event_number: [events]} — a number can legitimately collide only if
    Sherdog has two cards with the same number (it doesn't for Bellator, but
    the matcher refuses to guess if it ever does)."""
    by_num = {}
    for e in events:
        m = num_re.search(e['name'] or '')
        if m:
            by_num.setdefault(int(m.group(1)), []).append(e)
    return by_num

def event_number_from_title(title, num_re):
    m = num_re.search(title)
    return int(m.group(1)) if m else None

_HEADLINER_RE = re.compile(r'(?P<a>[^:|]+?)\s+vs?\.?\s+(?P<b>[^:|]+)$', re.I)

def match_event_by_headliners(title, events):
    """Fallback for un-numbered titles ('Bellator NYC: Sonnen vs Silva'):
    take the 'A vs B' tail and find the one event whose name carries both
    surnames. Sherdog names Bellator cards 'Bellator N - A vs. B', so this
    lands on the numbered event for the same night."""
    tail = title.split(':', 1)[-1]
    m = _HEADLINER_RE.search(tail.strip())
    if not m:
        return None, 'no event number / headliners in title'
    sa, sb = surname(_clean_name(m.group('a'))), surname(_clean_name(m.group('b')))
    if not sa or not sb:
        return None, 'no event number / headliners in title'
    hits = []
    for e in events:
        n = _fold(e['name'] or '')
        if re.search(rf'\b{re.escape(sa)}\b', n) and re.search(rf'\b{re.escape(sb)}\b', n):
            hits.append(e)
    if len(hits) == 1:
        return hits[0], 'matched (by headliners)'
    if not hits:
        return None, f'no event with headliners {sa} vs {sb}'
    return None, f'ambiguous headliners ({len(hits)} events): {sa} vs {sb}'

def match_event(title, by_num, num_re, events):
    num = event_number_from_title(title, num_re)
    if num is None:
        return match_event_by_headliners(title, events)
    cands = by_num.get(num, [])
    if len(cands) == 1:
        return cands[0], 'matched'
    if not cands:
        return None, f'no event numbered {num}'
    return None, f'ambiguous number {num} ({len(cands)} events)'

# ── Fight matching ────────────────────────────────────────────────────────────

def _tokens(name):
    return [t for t in re.sub(r"[.'\-]", ' ', _fold(name)).split() if t]

def _squash(name):
    return re.sub(r'[^a-z0-9]', '', _fold(name))

def _name_hits(title_name, fighter_id, fighter_names, alias_to_id):
    """Does a name from a VOD title refer to this fighter? Full name, alias,
    surname, or either side's surname appearing among the other's tokens
    (Pluto's "Carlos Leal Miranda" vs Sherdog's "Carlos Leal"; "Lindsay Van
    Zandt" vs "Lindsay VanZandt"). Loose on purpose — callers only compare
    against the fighters on one card."""
    tn = _fold(title_name)
    fn = _fold(fighter_names.get(fighter_id, ''))
    if not fn:
        return False
    if tn == fn or alias_to_id.get(tn) == fighter_id:
        return True
    if _squash(tn) == _squash(fn):
        return True
    st, sf = surname(tn), surname(fn)
    if st == sf:
        return True
    return sf in _tokens(tn) or st in _tokens(fn)

def _first_token(name):
    toks = _tokens(name)
    return toks[0] if toks else ''

# Transliteration drift between Pluto's and Sherdog's spelling of the same
# person ("Andrey Barberoshe" / "Andrei Barbarosa", "Nisim Rozalis" / "Nizim
# Rosales"). Only consulted when the opponent already resolved on the card.
_FUZZY_MIN = 0.75

def _fuzzy_same(title_name, fighter_name):
    a, b = _squash(title_name), _squash(fighter_name)
    if not a or not b:
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() >= _FUZZY_MIN

def match_fight_on_card(name_a, name_b, card_fights, fighter_names, alias_to_id):
    """Resolve 'A vs B' against the fights of one event. Requires exactly one
    fight whose pair matches (either order). Second tier for one side that
    resolves to exactly one fight while the other side is a nickname
    ("Patricio Pitbull" for Patricio Freire — first name agrees) or a
    respelling ("Lindsay Van Zandt" for Lindsey VanZandt — fuzzy)."""
    hits, partial = [], []
    for f in card_fights:
        f1, f2 = f['fighter1_id'], f['fighter2_id']
        if not f1 or not f2:
            continue
        n1, n2 = fighter_names.get(f1, ''), fighter_names.get(f2, '')
        a1 = _name_hits(name_a, f1, fighter_names, alias_to_id)
        b2 = _name_hits(name_b, f2, fighter_names, alias_to_id)
        a2 = _name_hits(name_a, f2, fighter_names, alias_to_id)
        b1 = _name_hits(name_b, f1, fighter_names, alias_to_id)
        if (a1 and b2) or (a2 and b1):
            hits.append(f)
            continue
        # One side anchored → accept the other on first name or fuzzy spelling.
        for anchored, other_name, other_db in ((a1, name_b, n2), (b2, name_a, n1),
                                                (a2, name_b, n1), (b1, name_a, n2)):
            if anchored and (_first_token(other_name) == _first_token(other_db)
                             or _fuzzy_same(other_name, other_db)):
                partial.append(f)
                break
    if len(hits) == 1:
        return hits[0], 'matched'
    if not hits and len(partial) == 1:
        return partial[0], 'matched (partial)'
    if not hits and not partial:
        return None, f'no fight on card for {name_a} vs {name_b}'
    return None, f'ambiguous ({len(hits) or len(partial)} fights on card): {name_a} vs {name_b}'

# ── Main ──────────────────────────────────────────────────────────────────────

def main(push=False):
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise ValueError('Missing SUPABASE_URL or SUPABASE_KEY in .env')

    print('Fetching Pluto series...')
    pluto = fetch_series([s['slug'] for s in SERIES])
    for s in SERIES:
        n = len(series_episodes(pluto[s['slug']])) if s['slug'] in pluto else 'MISSING'
        print(f'  {s["slug"]}: {n} episodes')

    print('Loading data from Supabase...')
    fighters = fetch_all('fighters', 'id,name')
    aliases  = fetch_all('fighter_aliases', 'alias,fighter_id', order='alias')
    fighter_names = {f['id']: f['name'] for f in fighters}
    alias_to_id   = {_fold(a['alias']): a['fighter_id'] for a in aliases}

    ev_matched, ev_unmatched = [], []
    fi_matched, fi_unmatched = [], []
    seen_event, seen_fight = set(), set()
    events_cache, fights_cache = {}, {}

    def org_data(org):
        if org not in events_cache:
            evs = fetch_all('events', 'id,name,date,pluto_url',
                            f'&organization=eq.{org}')
            ids = ','.join(str(e['id']) for e in evs) or '0'
            fts = fetch_all('fights', 'id,event_id,fighter1_id,fighter2_id,pluto_url',
                            f'&event_id=in.({ids})')
            by_event = {}
            for f in fts:
                by_event.setdefault(f['event_id'], []).append(f)
            events_cache[org] = evs
            fights_cache[org] = by_event
            print(f'  {org}: {len(evs)} events, {len(fts)} fights in scope')
        return events_cache[org], fights_cache[org]

    for cfg in SERIES:
        series = pluto.get(cfg['slug'])
        if not series:
            print(f'\n[WARN] Pluto returned no series for {cfg["slug"]} — skipped')
            continue
        events, fights_by_event = org_data(cfg['org'])
        by_num = build_event_index(events, cfg['num_re'])
        eps = series_episodes(series)
        print(f'\n=== {series["name"]} ({cfg["kind"]}, org={cfg["org"]}) — {len(eps)} episodes ===')

        for season, ep in eps:
            title = ep.get('name', '')
            url = episode_url(series, season, ep)

            if cfg['kind'] == 'event':
                event, reason = match_event(title, by_num, cfg['num_re'], events)
                if not event:
                    ev_unmatched.append((reason, title, url))
                    continue
                if event['id'] in seen_event:
                    # Pluto splits one night into two episodes (Bellator NYC +
                    # Bellator 180 at MSG); Sherdog has one event. First wins.
                    ev_unmatched.append((f'duplicate: event {event["id"]} already matched', title, url))
                    continue
                seen_event.add(event['id'])
                ev_matched.append({
                    'event_id': event['id'], 'event_name': event['name'],
                    'video_title': title, 'url': url, 'reason': reason,
                    'current': event.get('pluto_url'),
                })
                continue

            # kind == 'fight'
            parsed = parse_fight_title(title)
            if not parsed:
                fi_unmatched.append(('PARSE_FAIL', title, url))
                continue
            name_a, name_b, event_hint = parsed
            # The season number IS the event number; the title's "From
            # Bellator N" is the cross-check when the season is missing.
            # Pluto occasionally files a bout under the wrong season
            # ("... From Bellator 205" in season 223) — try the title's
            # number second.
            nums = []
            for n in (season if isinstance(season, int) else None,
                      event_number_from_title(event_hint, cfg['num_re'])):
                if n is not None and n not in nums:
                    nums.append(n)
            fight, event, reason = None, None, f'no unique event numbered {nums} ({event_hint})'
            for n in nums:
                cands = by_num.get(n, [])
                if len(cands) != 1:
                    continue
                event = cands[0]
                fight, reason = match_fight_on_card(
                    name_a, name_b, fights_by_event.get(event['id'], []),
                    fighter_names, alias_to_id)
                if fight:
                    break
                reason = f'{reason} @ {event["name"]}'
            if not fight:
                fi_unmatched.append((reason, title, url))
                continue
            if fight['id'] in seen_fight:
                fi_unmatched.append((f'duplicate: fight {fight["id"]} already matched', title, url))
                continue
            seen_fight.add(fight['id'])
            n1 = fighter_names.get(fight['fighter1_id'], '?')
            n2 = fighter_names.get(fight['fighter2_id'], '?')
            fi_matched.append({
                'fight_id': fight['id'], 'label': f'{n1} vs {n2} @ {event["name"]}',
                'video_title': title, 'url': url, 'reason': reason,
                'current': fight.get('pluto_url'),
            })

    # ── Write review file ─────────────────────────────────────────────────────
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as out:
        out.write('PLUTO TV MATCH RESULTS\n')
        out.write(f'Events  — matched: {len(ev_matched)}  unmatched: {len(ev_unmatched)}\n')
        out.write(f'Fights  — matched: {len(fi_matched)}  unmatched: {len(fi_unmatched)}\n')
        out.write('=' * 70 + '\n\n')

        out.write(f'MATCHED FULL EVENTS ({len(ev_matched)})\n' + '-' * 70 + '\n')
        for m in ev_matched:
            out.write(f"event_id: {m['event_id']}  [{m['reason']}]\n")
            out.write(f"event:    {m['event_name']}\n")
            out.write(f"video:    {m['video_title']}\n")
            out.write(f"url:      {m['url']}\n\n")

        out.write(f'\nMATCHED FIGHTS ({len(fi_matched)})\n' + '-' * 70 + '\n')
        for m in fi_matched:
            out.write(f"fight_id: {m['fight_id']}  [{m['reason']}]\n")
            out.write(f"fight:    {m['label']}\n")
            out.write(f"video:    {m['video_title']}\n")
            out.write(f"url:      {m['url']}\n\n")

        out.write(f'\nUNMATCHED FULL EVENTS ({len(ev_unmatched)})\n' + '-' * 70 + '\n')
        for reason, title, url in ev_unmatched:
            out.write(f'{reason:<40} | {title} | {url}\n')

        out.write(f'\nUNMATCHED FIGHTS ({len(fi_unmatched)})\n' + '-' * 70 + '\n')
        for reason, title, url in fi_unmatched:
            out.write(f'{reason:<40} | {title} | {url}\n')

    print(f'\nEvents — matched: {len(ev_matched)}  unmatched: {len(ev_unmatched)}')
    print(f'Fights — matched: {len(fi_matched)}  unmatched: {len(fi_unmatched)}')
    print(f'Saved to: {OUTPUT_FILE}')

    # Only rows whose stored URL differs are written — the audit triggers on
    # events/fights would otherwise log ~340 no-op updates every night.
    ev_changed = [m for m in ev_matched if m['current'] != m['url']]
    fi_changed = [m for m in fi_matched if m['current'] != m['url']]
    print(f'Changed vs DB — events: {len(ev_changed)}  fights: {len(fi_changed)}')

    if push:
        if ev_changed:
            print(f'\nPushing {len(ev_changed)} event URLs to Supabase...')
            ok = sum(update_pluto_url('events', m['event_id'], m['url']) for m in ev_changed)
            print(f'  updated {ok}/{len(ev_changed)} events')
        if fi_changed:
            print(f'Pushing {len(fi_changed)} fight URLs to Supabase...')
            ok = sum(update_pluto_url('fights', m['fight_id'], m['url']) for m in fi_changed)
            print(f'  updated {ok}/{len(fi_changed)} fights')
        if not ev_changed and not fi_changed:
            print('\nNothing to push — DB already up to date.')
    else:
        print('\nDry run — pass --push to write pluto_url columns')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--push', action='store_true',
                        help='Write matched URLs to events.pluto_url / fights.pluto_url')
    args = parser.parse_args()
    main(push=args.push)
