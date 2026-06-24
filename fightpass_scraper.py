#!/usr/bin/env python3
"""
UFC Fight Pass scraper -> Supabase

Maps Fight Pass event VODs (and the fights within each) to UFC events and
fights in our DB, storing the watch URLs in:
  events.fightpass_url           (main card replay)
  events.fightpass_prelims_url   (prelims replay)
  fights.fightpass_url           (URL of whichever card the fight is on)

Local-only — Fight Pass auth is tied to your account; can't run from
GitHub-Actions datacenter IPs. Same operational model as paramount_scraper.py
(Windows Task Scheduler).

  python fightpass_scraper.py          # dry run, writes fightpass_matches.txt
  python fightpass_scraper.py --push   # also write URLs to the DB

.env needs: FIGHTPASS_EMAIL, FIGHTPASS_PASSWORD, SUPABASE_URL, SUPABASE_KEY
"""

import os
import re
import sys
import json
import time
import argparse
import unicodedata
import requests
from datetime import date

# ── Load .env ─────────────────────────────────────────────────────────────────
_env = os.path.join(os.path.dirname(__file__), '.env')
if os.path.exists(_env):
    with open(_env, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip())

SUPABASE_URL  = os.environ.get('SUPABASE_URL', '').rstrip('/')
SUPABASE_KEY  = os.environ.get('SUPABASE_KEY', '')
FP_EMAIL      = os.environ.get('FIGHTPASS_EMAIL', '')
FP_PASSWORD   = os.environ.get('FIGHTPASS_PASSWORD', '')

# ── Fight Pass / IMG Gaming DCE constants ─────────────────────────────────────
API_BASE   = 'https://dce-frontoffice.imggaming.com'
WATCH_URL  = 'https://ufcfightpass.com/video/{}'   # /video/{vodId}
REALM      = 'dce.ufc'
X_API_KEY  = '857a1e5d-e35e-4fdf-805b-a87b6f8364bf'    # public client key
USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36')

# Series we want to crawl. Each episode = a full event VOD (event-level URL).
# matcher='title'           — default: match VOD title against event name.
# matcher='season_episode'  — Road to UFC: VOD titles are generic ("Episode 1"),
#                             so match by (seasonNumber, episodeNumber) →
#                             positional index in DB events for that season.
SERIES = [
    {'id': 2284, 'name': 'UFC Event Replays', 'organization': 'UFC',
     'matcher': 'title'},
    {'id': 2924, 'name': 'Road to UFC',       'organization': 'Road to UFC',
     'matcher': 'season_episode'},
]

# Top-level Fight Pass buckets to crawl for URLs. Two axes:
#   type  — PLAYLISTS    : bucket items are playlists, each containing VODs
#         — VOD_PLAYLIST : bucket items ARE the VODs (flat list)
#   level — 'fight'      : each VOD = one fight  → fights.fightpass_url
#         — 'event'      : each VOD = one event  → events.fightpass_url
#   orgs  — which DB organizations to consider for matching this bucket's VODs
PLAYLIST_BUCKETS = [
    # UFC — per-event playlists with individual fight VODs inside
    {'exid': 'HCpi', 'name': 'UFC REPLAYS - RECENT',
     'type': 'PLAYLISTS', 'level': 'fight', 'orgs': ['UFC']},
    # UFC — per-division playlists, every title fight ever
    {'exid': '9bTN', 'name': 'UFC Title Fight History',
     'type': 'PLAYLISTS', 'level': 'fight', 'orgs': ['UFC']},
    # PRIDE / Strikeforce / WEC — flat lists of full event-replay VODs
    {'exid': 'uj-A', 'name': 'Pride FC Event Replays',
     'type': 'VOD_PLAYLIST', 'level': 'event', 'orgs': ['PRIDE']},
    {'exid': '97Ms', 'name': 'StrikeForce Event Replays',
     'type': 'VOD_PLAYLIST', 'level': 'event', 'orgs': ['Strikeforce']},
    {'exid': '_7Cg', 'name': 'WEC Event Replays',
     'type': 'VOD_PLAYLIST', 'level': 'event', 'orgs': ['WEC']},
    # PRIDE / Strikeforce / WEC — flat lists of standalone per-fight VODs
    {'exid': 'ioe3', 'name': 'Pride FC: Classic Fights',
     'type': 'VOD_PLAYLIST', 'level': 'fight', 'orgs': ['PRIDE']},
    {'exid': 'JW-A', 'name': 'Strikeforce: Classic Fights',
     'type': 'VOD_PLAYLIST', 'level': 'fight', 'orgs': ['Strikeforce']},
    {'exid': 'cbRY', 'name': 'WEC: Classic Fights',
     'type': 'VOD_PLAYLIST', 'level': 'fight', 'orgs': ['WEC']},
    # PRIDE LEGENDS — per-fighter playlists, each with per-fight VODs
    {'exid': 'Md4s', 'name': 'PRIDE FC LEGENDS',
     'type': 'PLAYLISTS', 'level': 'fight', 'orgs': ['PRIDE']},
]

OUTPUT_FILE     = 'fightpass_matches.txt'
PROGRESS_FILE   = 'fightpass_seen_episodes.txt'
SCAN_PROGRESS   = 'fightpass_scan_progress.txt'

# Legacy standalone fight VODs (not in any walkable playlist) live in this
# ID range — Fight Pass uploaded a huge batch on 2019-03-14. Brute-force
# scan is the only way to discover them.
VOD_SCAN_START  = 25000
VOD_SCAN_END    = 60000

sys.stdout.reconfigure(encoding='utf-8')

# ── Auth + API session ────────────────────────────────────────────────────────

class FightPass:
    """Thin client: keeps a JWT, re-logs in on 401."""

    def __init__(self, email, password):
        self.email = email
        self.password = password
        self.session = requests.Session()
        self.session.headers.update({
            'Realm':      REALM,
            'x-api-key':  X_API_KEY,
            'app':        'dice',
            'User-Agent': USER_AGENT,
            'Origin':     'https://ufcfightpass.com',
            'Referer':    'https://ufcfightpass.com/',
        })
        self._jwt = None
        self._login()

    def _login(self):
        r = self.session.post(
            f'{API_BASE}/api/v2/login',
            headers={'Content-Type': 'application/json'},
            json={'id': self.email, 'secret': self.password}, timeout=30,
        )
        r.raise_for_status()
        self._jwt = r.json()['authorisationToken']
        self.session.headers['Authorization'] = f'Bearer {self._jwt}'

    def get(self, path, **params):
        url = f'{API_BASE}{path}'
        r = self.session.get(url, params=params or None, timeout=30)
        if r.status_code == 401:
            # JWT expired — re-login once and retry
            self._login()
            r = self.session.get(url, params=params or None, timeout=30)
        r.raise_for_status()
        return r.json()

    # ── catalog walkers ─────────────────────────────────────────────────────

    def series_seasons(self, series_id):
        """All seasons of a series — handles pagination via lastSeen."""
        last_seen = None
        while True:
            params = {}
            if last_seen is not None:
                params['lastSeen'] = last_seen
            d = self.get(f'/api/v4/series/{series_id}', **params)
            for s in d.get('seasons', []):
                yield s
            paging = d.get('paging') or {}
            if not paging.get('moreDataAvailable'):
                break
            last_seen = paging.get('lastSeen')
            if last_seen is None:
                break
            time.sleep(0.2)

    def season_episodes(self, season_id):
        """All episodes of a season — handles pagination."""
        last_seen = None
        while True:
            params = {}
            if last_seen is not None:
                params['lastSeen'] = last_seen
            d = self.get(f'/api/v4/season/{season_id}', **params)
            for ep in d.get('episodes', []):
                yield ep
            paging = d.get('paging') or {}
            if not paging.get('moreDataAvailable'):
                break
            last_seen = paging.get('lastSeen')
            if last_seen is None:
                break
            time.sleep(0.2)

    def vod(self, vod_id):
        """VOD detail with subEvents (the fights on the card)."""
        return self.get(f'/api/v4/vod/{vod_id}')

    def vod_quiet(self, vod_id):
        """vod() but returns None on 404 instead of raising — used by the
        brute-force scanner which expects holes in the ID range."""
        url = f'{API_BASE}/api/v4/vod/{vod_id}'
        r = self.session.get(url, timeout=15)
        if r.status_code == 401:
            self._login()
            r = self.session.get(url, timeout=15)
        if r.status_code == 404:
            return None
        if not r.ok:
            return None
        return r.json()

    def vesper_search(self, query):
        """Fight Pass's UI search hits a separate service (vesper) instead of
        any /api/v4/* endpoint. Returns [(vod_id, title), ...] of VODs whose
        title matches the query — catches per-fight VODs that aren't in any
        walkable playlist (e.g. pre-2023 standalone fight VODs like Zahabi
        vs Vieira UFC Fight Night vod 53279)."""
        url = 'https://search.dce-prod.dicelaboratory.com/search'
        r = self.session.get(url, params={'query': query}, timeout=20)
        if r.status_code == 401:
            self._login()
            r = self.session.get(url, params={'query': query}, timeout=20)
        if not r.ok:
            return []
        # Each result card has an `action.data` blob like
        #   {"type": "VOD", "title": "...", "id": "VOD#241537", ...}
        # Walk the JSON tree and pull title + id from those nodes. (An
        # earlier regex-based version paired text from a PLAYLIST card with
        # the id from the next VOD card and silently produced bad titles.)
        try:
            tree = r.json()
        except ValueError:
            return []
        out = []
        def walk(node):
            if isinstance(node, dict):
                if node.get('type') == 'VOD':
                    nid = node.get('id') or ''
                    if isinstance(nid, str) and nid.startswith('VOD#'):
                        try:
                            out.append((int(nid[4:]), node.get('title') or ''))
                        except ValueError:
                            pass
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)
        walk(tree)
        # Dedupe (a single VOD can appear in multiple result sections).
        seen = set()
        unique = []
        for vid, title in out:
            if vid in seen:
                continue
            seen.add(vid)
            unique.append((vid, title))
        return unique

    def bucket_items(self, exid):
        """All items in a top-level browse bucket (paginated)."""
        last_seen = None
        while True:
            params = {}
            if last_seen is not None:
                params['lastSeen'] = last_seen
            d = self.get(f'/api/v4/content/bucket/{exid}', **params)
            for it in d.get('contentList', []):
                yield it
            paging = d.get('paging') or {}
            if not paging.get('moreDataAvailable'):
                break
            nxt = paging.get('lastSeen')
            if nxt is None or nxt == last_seen:
                break
            last_seen = nxt
            time.sleep(0.2)

    def playlist_vods(self, playlist_id):
        """All VODs inside a playlist (paginated)."""
        last_seen = None
        while True:
            params = {}
            if last_seen is not None:
                params['lastSeen'] = last_seen
            d = self.get(f'/api/v4/playlist/{playlist_id}', **params)
            for v in d.get('vods', []):
                yield v
            paging = d.get('paging') or {}
            if not paging.get('moreDataAvailable'):
                break
            nxt = paging.get('lastSeen')
            if nxt is None or nxt == last_seen:
                break
            last_seen = nxt
            time.sleep(0.2)

# ── Supabase helpers ──────────────────────────────────────────────────────────

def _sb_headers():
    return {
        'apikey':        SUPABASE_KEY,
        'Authorization': f'Bearer {SUPABASE_KEY}',
        'Content-Type':  'application/json',
        'Prefer':        'return=minimal',
    }

def fetch_all(table, select='*', extra=''):
    PAGE, offset, out = 1000, 0, []
    while True:
        r = requests.get(
            f'{SUPABASE_URL}/rest/v1/{table}'
            f'?select={select}&limit={PAGE}&offset={offset}{extra}',
            headers=_sb_headers())
        if not r.ok:
            print(f'  [WARN] fetch {table} failed ({r.status_code})')
            break
        batch = r.json()
        out.extend(batch)
        if len(batch) < PAGE:
            break
        offset += PAGE
    return out

def update_event(event_id, payload):
    r = requests.patch(f'{SUPABASE_URL}/rest/v1/events?id=eq.{event_id}',
                       headers=_sb_headers(), data=json.dumps(payload))
    return r.status_code in (200, 204)

def update_fight(fight_id, payload):
    r = requests.patch(f'{SUPABASE_URL}/rest/v1/fights?id=eq.{fight_id}',
                       headers=_sb_headers(), data=json.dumps(payload))
    return r.status_code in (200, 204)

# ── Matching ──────────────────────────────────────────────────────────────────

def _fold_accents(s):
    """'Jędrzejczyk' -> 'Jedrzejczyk' — Fight Pass sends proper Unicode,
    ufcstats stores ASCII versions; folding lets them match."""
    if not s:
        return s
    return ''.join(c for c in unicodedata.normalize('NFD', s)
                   if unicodedata.category(c) != 'Mn')

def _norm(name):
    return re.sub(r'[^a-z0-9]+', ' ', (name or '').lower()).strip()

def classify_episode(title):
    """Returns (variant, base_title) — variant is 'main' or 'prelims'.
    Matches Prelims / Preliminaries / Preliminares (Spanish) — not just the
    exact word 'Prelims', so Spanish prelims aren't misread as the main card."""
    if re.search(r'\bprelim', title or '', re.I):
        # Strip a trailing "(Fight Pass) Prelim..." so the base matches the event
        base = re.sub(r'\s*[:\-]?\s*(fight\s+pass\s+)?prelim\w*\s*$', '',
                      title, flags=re.IGNORECASE).strip()
        return 'prelims', base
    return 'main', title


def _is_spanish(title):
    """Spanish-language VOD (e.g. "(ESPAÑOL) ..."). English is canonical, so
    these are skipped for event slots rather than taking main/prelims."""
    return bool(re.search(r'espa[nñ]ol|\(spanish\)', title or '', re.I))

def match_event(fp_title, events_by_norm):
    """fp_title is the cleaned (no Prelims) episode title."""
    return events_by_norm.get(_norm(fp_title))

def build_season_episode_map(org_events):
    """For series matched by (seasonNumber, episodeNumber) — currently just
    Road to UFC. Group DB events by season parsed from name (defaults to
    season 1 when no 'Season N' substring), sort by (date, trailing-number
    in name), then position-map them.

    Returns {(season_number, episode_number): event_dict}."""
    from datetime import datetime
    def _parse_date(s):
        try:
            return datetime.strptime(s, '%B %d, %Y')
        except (ValueError, TypeError):
            return None
    def _season(name):
        m = re.search(r'Season\s+(\d+)', name or '', re.I)
        return int(m.group(1)) if m else 1
    def _trailing_num(name):
        m = re.search(r'(\d+)\s*$', name or '')
        return int(m.group(1)) if m else 0

    by_season = {}
    for e in org_events:
        date = _parse_date(e.get('date'))
        if not date:
            continue
        by_season.setdefault(_season(e['name']), []).append(
            (date, _trailing_num(e['name']), e))

    result = {}
    for season_n, items in by_season.items():
        items.sort(key=lambda x: (x[0], x[1]))
        for i, (_d, _n, e) in enumerate(items):
            result[(season_n, i + 1)] = e
    return result

# Noise words that don't carry identifying info — stripped when comparing
# Fight Pass titles to Sherdog event names. Different sources omit different
# filler ("Pride: Shockwave 2006" vs "Pride FC - Shockwave 2006"), so we
# strip these before comparing.
_NOISE_TOKENS = frozenset({
    'the', 'a', 'an', 'and', 'fc', 'mma',
    'world', 'extreme', 'cagefighting',   # Sherdog's WEC-NN placeholder filler
})

def _core_tokens(name):
    """Normalized + noise-stripped token list. 'Pride FC - Shockwave 2006' →
    ['pride', 'shockwave', '2006']."""
    return [t for t in _norm(name).split() if t not in _NOISE_TOKENS]

def match_event_fuzzy(fp_title, events_for_org, events_by_norm):
    """Multi-strategy event matcher for non-UFC orgs where Fight Pass and
    Sherdog disagree on title format. Tries in order:
      1. Exact normalized match (fast path).
      2. Core-token equality (after noise word strip).
      3. DB tokens form a contiguous prefix of FP tokens (Fight Pass adds
         a subtitle like 'Challengers 20: Britt vs Sayers').
      4. Leading-N tokens uniquely identify exactly one event in the org
         (e.g. 'WEC 35' → only one DB event starts with that).
    Returns the event dict or None."""
    nt = _norm(fp_title)
    if nt in events_by_norm:
        return events_by_norm[nt]

    fp_core = _core_tokens(fp_title)
    fp_core_str = ' '.join(fp_core)
    fp_core_set = set(fp_core)
    for e in events_for_org:
        db_core = _core_tokens(e['name'])
        if not db_core:
            continue
        # (2) core-token sequence equality
        if db_core == fp_core:
            return e
        # (2b) core-token set equality (handles minor word reorders)
        if set(db_core) == fp_core_set and len(db_core) == len(fp_core):
            return e
        # (3) DB core is a contiguous prefix of FP core
        if len(db_core) >= 2 and fp_core[:len(db_core)] == db_core:
            return e

    # (4) Leading-N: first 2..4 tokens of FP uniquely identify a DB event
    for n in (4, 3, 2):
        if len(fp_core) < n:
            continue
        lead = ' '.join(fp_core[:n])
        cands = [e for e in events_for_org
                 if ' '.join(_core_tokens(e['name'])[:n]) == lead]
        if len(cands) == 1:
            return cands[0]
    return None

def resolve_fighter(name, by_name, by_alias, by_last):
    nl = (name or '').lower().strip()
    for cand in (nl, _fold_accents(nl)):   # try exact, then ASCII-folded
        if not cand:
            continue
        fid = by_name.get(cand)
        if fid:
            return [fid]
        fid = by_alias.get(cand)
        if fid:
            return [fid]
    last = surname(_fold_accents(nl) or nl)
    return by_last.get(last, [])

_NAME_SUFFIX = frozenset({'jr', 'sr', 'ii', 'iii', 'iv', 'v'})

def surname(name):
    """Last-name token for the fallback index, ignoring trailing generational
    suffixes (Jr., Sr., II–V). Without this every '… Jr.' keys under 'jr.', so a
    clip like 'Michael Aswell Jr.' collides with every other Junior on lookup.
    Must be used for both index construction and lookup so the keys line up."""
    toks = (name or '').split()
    while len(toks) > 1 and toks[-1].lower().rstrip('.') in _NAME_SUFFIX:
        toks = toks[:-1]
    return toks[-1].lower() if toks else ''

def _event_is_future(ev):
    """True if the event is confirmed to be in the future. A full-fight VOD can't
    exist for a bout that hasn't happened, so such candidates are dropped before
    matching. Undated/unparseable events return False (kept)."""
    s = ((ev or {}).get('date') or '')[:10]
    try:
        return date.fromisoformat(s) > date.today()
    except ValueError:
        return False

def _drop_future(candidates, ev_by_id):
    """Return candidates whose event isn't in the future; if that leaves none,
    return [] so the caller logs it unmatched rather than picking a future bout."""
    return [f for f in candidates
            if not _event_is_future(ev_by_id.get(f['event_id'], {}))]

def _name_tokens(name):
    """Accent-folded lowercase token set, name-suffixes dropped. Token-set
    overlap tolerates spelling/nickname drift ('Cannon'~'Canon' share nothing,
    but the opponent + a shared token still pin the fight within one event)."""
    s = _fold_accents(name or '').lower()
    s = re.sub(r'[^a-z0-9 ]', ' ', s)
    return {t for t in s.split() if t and t not in _NAME_SUFFIX}

def event_anchored_fight(a, b, hint, events, events_by_norm, fights_by_event, fighter_names):
    """Fallback for Fight Pass name misspellings/nicknames the exact resolver
    misses: pin the event from the title's event hint, then fuzzy-match the
    fight within that event's (few) bouts by name-token overlap. Requires a
    clear, unambiguous winner so it won't mis-assign."""
    if not hint:
        return None
    ev = match_event_fuzzy(hint, events, events_by_norm)
    if not ev:
        return None
    ta, tb = _name_tokens(a), _name_tokens(b)
    if not ta or not tb:
        return None
    best, best_score, tie = None, 0, False
    for fi in fights_by_event.get(ev['id'], []):
        n1 = _name_tokens(fighter_names.get(fi['fighter1_id'], ''))
        n2 = _name_tokens(fighter_names.get(fi['fighter2_id'], ''))
        score = max(len(ta & n1) + len(tb & n2), len(ta & n2) + len(tb & n1))
        if score > best_score:
            best, best_score, tie = fi, score, False
        elif score == best_score and best and fi['id'] != best['id']:
            tie = True
    if best and best_score >= 2 and not tie:
        return best
    return None

# Fight Pass per-fight VOD titles look like:
#   "Conor McGregor vs Chad Mendes UFC 189"
#   "Jose Aldo vs. Chan Sung Jung UFC 163"
#   "Roman Kopylov vs Marco Tulio UFC 328"
#   "Pat Sabatini vs William Gomis UFC Vegas 117"
#   "Jack Della Maddalena vs Islam Makhachev VeChain UFC 322"  (sponsor)
#   "Alexandre Pantoja vs Kai Asakura - UFC 310"               (dash sep)
#   "Themba Gorimbo vs Niko Price at UFC Fight Night ..."      (word sep)
# Multi-word phrases first so the regex doesn't lock onto a shorter prefix.
_EVENT_PREFIX = (
    r'(?:Dana\s+White|Contender\s+Series|DWCS|King\s+of\s+the\s+Cage|'
    # CFFC's other Fight Pass naming — must precede the Fury FC rule below so
    # "Cage Fury FC" is read as CFFC, not Fury FC.
    r'Cage\s+Fury\s+FC|'
    # "(?<!Cage\s)Fury FC" so a bare "Fury FC" is the Texas org, not CFFC.
    r'(?<!Cage\s)Fury\s+FC|Titan\s+FC|'
    r'Vale\s+Tudo\s+Japan|Cage\s+Warriors|Noche\s+UFC|Invicta(?:\s+FC)?|'
    r'UFC|Strikeforce|PRIDE|WEC|Bellator|Affliction|VTJ|'
    r'LFA|CFFC|Pancrase|Shooto|KOTC|EliteXC)'
)
# Optional crud between fighter2 and the event prefix: sponsor + a separator
_CRUD = r'(?:[-—–:]|at)?\s*(?:VeChain|ESPN\+?|TUDN|DraftKings)?\s*'
_FIGHT_VOD_RE = re.compile(
    r'^(?P<a>.+?)\s+vs\.?\s+(?P<b>.+?)\s+'
    + _CRUD + r'(?P<event>' + _EVENT_PREFIX + r'\b.*)$',
    re.IGNORECASE,
)

# Alternate-broadcast tags (Snoop Dogg's "SnoopCast" alt commentary on DWCS).
# These sit between the fighter names and the event keyword in VOD titles
# ("X vs Y SnoopCast: Contender Series"), so they get absorbed into fighter B
# and break matching. Strip them before parsing, and flag them so a SnoopCast
# VOD is only used as a fallback when no standard video exists for the fight.
_ALT_RE = re.compile(r'\s*\b(?:snoop\s*cast|snoop\s*dogg)\b\s*:?\s*', re.IGNORECASE)

def is_alt_broadcast(title):
    return bool(title and _ALT_RE.search(title))

def parse_fight_vod_title(title):
    """Return (fighter_a, fighter_b, event_hint) or (None, None, None)."""
    if not title:
        return None, None, None
    m = _FIGHT_VOD_RE.match(_ALT_RE.sub(' ', title).strip())
    if not m:
        return None, None, None
    return m.group('a').strip(), m.group('b').strip(), m.group('event').strip()

def find_event_fight(fighter_pair_ids, event_id, fights_by_event):
    """Inside a known event, find the fight matching this fighter pair."""
    pair = frozenset(fighter_pair_ids)
    for fi in fights_by_event.get(event_id, []):
        if frozenset((fi['fighter1_id'], fi['fighter2_id'])) == pair:
            return fi
    return None

# ── Progress tracking ─────────────────────────────────────────────────────────

def load_seen():
    if not os.path.exists(PROGRESS_FILE):
        return set()
    with open(PROGRESS_FILE) as f:
        return {line.strip() for line in f if line.strip()}

def mark_seen(vod_id):
    with open(PROGRESS_FILE, 'a') as f:
        f.write(f'{vod_id}\n')

def load_scan_progress():
    if not os.path.exists(SCAN_PROGRESS):
        return None
    with open(SCAN_PROGRESS) as f:
        return int(f.read().strip())

def save_scan_progress(vid):
    with open(SCAN_PROGRESS, 'w') as f:
        f.write(str(vid))

# ── Main ──────────────────────────────────────────────────────────────────────

def main(push=False, force=False, scan_vods=False,
         scan_start=None, scan_end=None, search_vesper=False, only_orgs=None):
    for var in ('SUPABASE_URL', 'SUPABASE_KEY', 'FIGHTPASS_EMAIL', 'FIGHTPASS_PASSWORD'):
        if not os.environ.get(var):
            print(f'Missing {var} in .env'); sys.exit(1)

    print('Loading DB...')
    fighters = fetch_all('fighters', 'id,name')
    aliases  = fetch_all('fighter_aliases', 'alias,fighter_id')
    events   = fetch_all('events', 'id,name,date,organization')
    fights   = fetch_all('fights', 'id,event_id,fighter1_id,fighter2_id')

    # Lookups — store both original-cased and accent-folded keys so a Fight
    # Pass name with diacritics matches an ASCII ufcstats record.
    by_name, by_alias, by_last = {}, {}, {}
    for f in fighters:
        nl = f['name'].lower()
        by_name.setdefault(nl, f['id'])
        by_name.setdefault(_fold_accents(nl), f['id'])
        last = surname(nl)
        by_last.setdefault(last, []).append(f['id'])
        by_last.setdefault(_fold_accents(last), []).append(f['id'])
    for a in aliases:
        al = a['alias'].lower()
        by_alias.setdefault(al, a['fighter_id'])
        by_alias.setdefault(_fold_accents(al), a['fighter_id'])
    fighter_names = {f['id']: f['name'] for f in fighters}
    org_by_event = {e['id']: e.get('organization') for e in events}

    fight_lookup = {}   # frozenset({f1_id, f2_id}) -> [fights]
    fights_by_event = {}
    for fi in fights:
        if fi['fighter1_id'] and fi['fighter2_id']:
            fight_lookup.setdefault(
                frozenset((fi['fighter1_id'], fi['fighter2_id'])), []).append(fi)
            fights_by_event.setdefault(fi['event_id'], []).append(fi)

    seen = set() if force else load_seen()
    print(f'  {len(fighters)} fighters, {len(events)} events, {len(fights)} fights')
    print(f'  {len(seen)} episodes already seen (skip on re-run)\n')

    fp = FightPass(FP_EMAIL, FP_PASSWORD)

    matched_events  = []   # (event_id, variant, url, ep_title)
    matched_fights  = []   # (fight_id, url, label, source) — source = 'event' or 'fight'
    unmatched_eps   = []
    unmatched_subs  = []
    per_fight_unmatched = []  # (vod_id, vod_title, reason)
    matched_vod_ids = []   # only flush to seen-file if --push (so dry runs
                           # don't poison future push runs)

    ev_by_id = {e['id']: e for e in events}
    all_events_by_norm = {_norm(e['name']): e for e in events}  # global, for vesper fallback

    for series in SERIES:
        org_events = [e for e in events if e['organization'] == series['organization']]
        events_by_norm = {_norm(e['name']): e for e in org_events}
        # Position-based matcher (Road to UFC): build (season, episode) → event.
        position_map = (build_season_episode_map(org_events)
                        if series.get('matcher') == 'season_episode' else {})
        print(f'=== {series["name"]} (matching against {len(org_events)} '
              f'{series["organization"]} events) ===')

        for season in fp.series_seasons(series['id']):
            print(f'  season {season["id"]} {season["title"]!r}  '
                  f'(episodes={season.get("episodeCount")})')
            for ep in fp.season_episodes(season['id']):
                vod_id = str(ep['id'])
                if vod_id in seen:
                    continue

                title   = ep.get('title', '')
                if _is_spanish(title):
                    continue   # English versions are canonical for event slots
                variant, base = classify_episode(title)
                url     = WATCH_URL.format(vod_id)

                if series.get('matcher') == 'season_episode':
                    ep_info = ep.get('episodeInformation') or {}
                    sn = ep_info.get('seasonNumber')
                    # Title's "Episode N" is reliable; API's episodeNumber
                    # is sometimes broken (RTUFC S3: returns 1,3,5,7,9 for
                    # a 5-episode season). Prefer title, fall back to API
                    # for episodes whose title has no "Episode N" (e.g.
                    # "Road to UFC S2: Finals").
                    en_match = re.search(r'Episode\s+(\d+)', title, re.I)
                    en = int(en_match.group(1)) if en_match else ep_info.get('episodeNumber')
                    ev = position_map.get((sn, en)) if sn and en else None
                else:
                    # Exact first (fast). Fall back to fuzzy — handles the
                    # prelims case where the title strips down to just
                    # "UFC 304" and needs leading-token match against
                    # "UFC 304: Edwards vs. Muhammad 2" in DB.
                    ev = match_event(base, events_by_norm)
                    if not ev:
                        ev = match_event_fuzzy(base, org_events, events_by_norm)

                if not ev:
                    unmatched_eps.append((vod_id, title, url))
                    # Don't mark seen — re-check on next run; may match after
                    # matcher improvements or new aliases.
                    continue

                matched_events.append((ev['id'], variant, url, title))
                matched_vod_ids.append(vod_id)

                # Pull VOD detail for subEvents (per-fight matching).
                try:
                    detail = fp.vod(vod_id)
                except Exception as e:
                    print(f'    [WARN] vod {vod_id}: {e}')
                    continue
                sub_events = detail.get('subEvents') or []

                event_fights = fights_by_event.get(ev['id'], [])
                event_fight_keys = {
                    frozenset((f['fighter1_id'], f['fighter2_id'])): f
                    for f in event_fights
                }

                for sub in sub_events:
                    parts = (sub.get('details') or {}).get('participants') or []
                    if len(parts) != 2:
                        continue
                    n1, n2 = parts[0].get('name'), parts[1].get('name')
                    ids1 = resolve_fighter(n1, by_name, by_alias, by_last)
                    ids2 = resolve_fighter(n2, by_name, by_alias, by_last)
                    matched_fight = None
                    for a in ids1:
                        for b in ids2:
                            f = event_fight_keys.get(frozenset((a, b)))
                            if f:
                                matched_fight = f
                                break
                        if matched_fight:
                            break
                    if not matched_fight:
                        unmatched_subs.append(
                            (vod_id, ev['name'], f'{n1} vs {n2}'))
                        # NB: don't push the EPISODE url onto matched_fight —
                        # that puts a multi-fight VOD link on each individual
                        # fight (e.g. 'UFC 323: Fight Pass Prelims' VOD on all
                        # 5 fights in it). fights.fightpass_url is reserved
                        # for true per-fight VODs (passes 2 + 3); the app
                        # falls back to events.fightpass_url when null.

                time.sleep(0.2)
            time.sleep(0.3)

    # ── PASS 2: walk PLAYLIST_BUCKETS ───────────────────────────────────────
    # Each bucket is either:
    #   level='event' — VODs are full event replays → match title to event,
    #                   push events.fightpass_url (non-UFC orgs only — UFC
    #                   events already covered by pass 1's series walk).
    #   level='fight' — VODs are per-fight, titles like "A vs B EVENT" →
    #                   parse, resolve fighters, match to a DB fight.
    # Buckets are typed PLAYLISTS (collection of playlists, each containing
    # VODs) or VOD_PLAYLIST (the bucket IS the VOD list).
    # Hard-skip non-content VODs (these can't be event-level or per-fight)
    SKIP_NON_CONTENT = ('press conference', 'weigh-in', 'weighin',
                        'pre-fight show', 'post-fight show', 'español', 'espanol')
    # Legacy alias still used by passes 3 and 4 below
    SKIP_KEYWORDS = SKIP_NON_CONTENT + ('fight pass prelims',
                                       ': prelims', ' prelims')

    def walk_bucket_vods(bucket):
        """Yield (vod_dict, parent_playlist_title or None) for every VOD in
        the bucket. Handles both PLAYLISTS and VOD_PLAYLIST bucket types."""
        if bucket['type'] == 'VOD_PLAYLIST':
            for it in fp.bucket_items(bucket['exid']):
                yield it, None
        else:  # PLAYLISTS
            for pl in fp.bucket_items(bucket['exid']):
                pl_title = pl.get('title') or ''
                for v in fp.playlist_vods(pl['id']):
                    yield v, pl_title

    for bucket in PLAYLIST_BUCKETS:
        print(f'\n=== bucket {bucket["exid"]}  {bucket["name"]} '
              f'({bucket["type"]}, level={bucket["level"]}, orgs={bucket["orgs"]}) ===')
        # Org-scoped event lookup — prevents a Sakuraba PRIDE fight matching
        # a different-org event with similar name (rare but real).
        bucket_events = [e for e in events
                         if e.get('organization') in bucket['orgs']]
        bucket_events_by_norm = {_norm(e['name']): e for e in bucket_events}
        bucket_event_ids = {e['id'] for e in bucket_events}

        count = 0
        for v, parent_pl_title in walk_bucket_vods(bucket):
            count += 1
            vod_id = str(v['id'])
            v_title = v.get('title') or ''
            if vod_id in seen:
                continue
            tl = v_title.lower()
            if any(kw in tl for kw in SKIP_NON_CONTENT):
                continue

            if bucket['level'] == 'event':
                # Match VOD title to one of this bucket's-org events.
                # Use fuzzy matcher — Fight Pass and Sherdog disagree on
                # title format ("PRIDE: Shockwave 2006" vs "Pride FC -
                # Shockwave 2006").
                ev = match_event_fuzzy(v_title, bucket_events,
                                       bucket_events_by_norm)
                if ev:
                    url = WATCH_URL.format(vod_id)
                    matched_events.append((ev['id'], 'main', url, v_title))
                    matched_vod_ids.append(vod_id)
                else:
                    unmatched_eps.append(
                        (vod_id, v_title, WATCH_URL.format(vod_id)))
                continue

            # level == 'fight': try event-level matching FIRST for prelims
            # VODs and language-prefixed main card VODs. These appear in
            # the HCpi playlist for an event that isn't in any series (e.g.
            # vod 917032 'UFC Fight Night: Bautista vs Oliveira Prelims'
            # has episodeInformation=None, so pass 1 misses it).
            # Strip language prefix like '(ESPAÑOL) ' for matching purposes.
            base_for_event = re.sub(r'^\([^)]+\)\s*', '', v_title).strip()
            if re.search(r'\bprelims\b', tl):
                # Strip ": Prelims" / " Fight Pass Prelims" suffix
                base = re.sub(r'\s*:?\s*(?:fight\s+pass\s+)?\bprelims\b\s*$',
                              '', base_for_event, flags=re.IGNORECASE).strip()
                ev = (match_event_fuzzy(base, bucket_events, bucket_events_by_norm)
                      if base else None)
                if ev:
                    url = WATCH_URL.format(vod_id)
                    matched_events.append((ev['id'], 'prelims', url, v_title))
                    matched_vod_ids.append(vod_id)
                continue   # prelims VODs are never per-fight

            scoped_event = (match_event(parent_pl_title, bucket_events_by_norm)
                            if parent_pl_title else None)
            a, b, hint = parse_fight_vod_title(v_title)
            if not a or not b:
                # Not a per-fight VOD. Might be a main card event VOD that
                # isn't in any series (e.g. the language-prefixed one).
                ev = (match_event_fuzzy(base_for_event, bucket_events,
                                        bucket_events_by_norm)
                      if base_for_event else None)
                if ev:
                    url = WATCH_URL.format(vod_id)
                    matched_events.append((ev['id'], 'main', url, v_title))
                    matched_vod_ids.append(vod_id)
                continue
            ids_a = resolve_fighter(a, by_name, by_alias, by_last)
            ids_b = resolve_fighter(b, by_name, by_alias, by_last)
            if not ids_a or not ids_b:
                per_fight_unmatched.append(
                    (vod_id, v_title, 'fighter not in DB'))
                continue
            # Candidate fights w/ this pair — filtered to bucket's orgs.
            cands_map = {}
            for x in ids_a:
                for y in ids_b:
                    for f in fight_lookup.get(frozenset((x, y)), []):
                        if f['event_id'] in bucket_event_ids:
                            cands_map[f['id']] = f
            candidates = list(cands_map.values())
            if not candidates:
                per_fight_unmatched.append(
                    (vod_id, v_title, 'no DB fight for this pair'))
                continue
            non_future = _drop_future(candidates, ev_by_id)
            if not non_future:
                per_fight_unmatched.append(
                    (vod_id, v_title, 'only future-dated bout for this pair'))
                continue
            candidates = non_future
            picked = None
            if scoped_event:
                for f in candidates:
                    if f['event_id'] == scoped_event['id']:
                        picked = f; break
            if not picked and len(candidates) == 1:
                picked = candidates[0]
            if not picked and hint:
                # Fast path: hint normalized as substring of event name.
                hint_n = _norm(hint)
                for f in candidates:
                    ev = ev_by_id.get(f['event_id'])
                    if ev and hint_n and hint_n in _norm(ev['name']):
                        picked = f; break
                # Fall back to fuzzy event matching on the hint — handles
                # noise-word differences like "PRIDE: Final Conflict 2004"
                # (FP hint) vs "Pride FC - Final Conflict 2004" (DB name).
                if not picked:
                    cand_events = [ev_by_id[f['event_id']] for f in candidates
                                   if f['event_id'] in ev_by_id]
                    cand_by_norm = {_norm(e['name']): e for e in cand_events}
                    matched_ev = match_event_fuzzy(hint, cand_events, cand_by_norm)
                    if matched_ev:
                        for f in candidates:
                            if f['event_id'] == matched_ev['id']:
                                picked = f; break
            if picked:
                ev = ev_by_id.get(picked['event_id'])
                url = WATCH_URL.format(vod_id)
                label = f'{a} vs {b}  @ {ev["name"] if ev else "?"}'
                matched_fights.append((picked['id'], url, label,
                                       'fight_alt' if is_alt_broadcast(v_title) else 'fight'))
                matched_vod_ids.append(vod_id)
            else:
                per_fight_unmatched.append(
                    (vod_id, v_title,
                     f'{len(candidates)} candidates; hint={hint!r}'))
        print(f'  walked {count} VODs')
        time.sleep(0.2)

    # ── PASS 3: brute-force VOD-ID scan for standalone fight VODs ──────────
    # A huge batch of pre-2019 standalone fight VODs (e.g. video/30613 =
    # Holloway vs McGregor at UFC Fight Night: Shogun vs Sonnen) exist on
    # Fight Pass but don't appear in any playlist bucket we walk. The only
    # way to find them is to walk VOD IDs in the legacy range, parse titles
    # and match to fights. State persists in fightpass_scan_progress.txt so
    # repeated runs only check NEW IDs.
    if scan_vods:
        scan_start_id = scan_start or load_scan_progress() or VOD_SCAN_START
        scan_end_id   = scan_end or VOD_SCAN_END
        print(f'\n=== PASS 3: brute-force VOD scan {scan_start_id}..{scan_end_id} ===')
        scanned = 0
        for vid in range(scan_start_id, scan_end_id + 1):
            vod_id = str(vid)
            if vod_id in seen:
                continue
            try:
                detail = fp.vod_quiet(vid)
            except Exception as e:
                print(f'  [WARN] vod {vid}: {e}')
                continue
            scanned += 1
            if scanned % 200 == 0:
                save_scan_progress(vid)
                print(f'  ...scanned {scanned} (now at id={vid})')
            if not detail:
                continue
            v_title = detail.get('title') or ''
            tl = v_title.lower()
            if any(kw in tl for kw in SKIP_KEYWORDS):
                continue
            a, b, hint = parse_fight_vod_title(v_title)
            if not a or not b:
                continue
            ids_a = resolve_fighter(a, by_name, by_alias, by_last)
            ids_b = resolve_fighter(b, by_name, by_alias, by_last)
            if not ids_a or not ids_b:
                per_fight_unmatched.append(
                    (vod_id, v_title, 'scan: fighter not in DB'))
                continue
            cands_map = {}
            for x in ids_a:
                for y in ids_b:
                    for f in fight_lookup.get(frozenset((x, y)), []):
                        cands_map[f['id']] = f
            candidates = list(cands_map.values())
            if not candidates:
                per_fight_unmatched.append(
                    (vod_id, v_title, 'scan: no DB fight for this pair'))
                continue
            non_future = _drop_future(candidates, ev_by_id)
            if not non_future:
                per_fight_unmatched.append(
                    (vod_id, v_title, 'scan: only future-dated bout for this pair'))
                continue
            candidates = non_future
            picked = None
            if len(candidates) == 1:
                picked = candidates[0]
            elif hint:
                hint_n = _norm(hint)
                for f in candidates:
                    ev = ev_by_id.get(f['event_id'])
                    if ev and hint_n and hint_n in _norm(ev['name']):
                        picked = f
                        break
            if picked:
                ev = ev_by_id.get(picked['event_id'])
                url = WATCH_URL.format(vod_id)
                label = f'{a} vs {b}  @ {ev["name"] if ev else "?"} [scan]'
                matched_fights.append((picked['id'], url, label,
                                       'fight_alt' if is_alt_broadcast(v_title) else 'fight'))
                matched_vod_ids.append(vod_id)
            else:
                per_fight_unmatched.append(
                    (vod_id, v_title,
                     f'scan: {len(candidates)} candidates; hint={hint!r}'))
            time.sleep(0.05)
        save_scan_progress(scan_end_id)
        print(f'  scan complete — {scanned} VODs checked')

    # ── PASS 4: vesper search by fighter name ──────────────────────────────
    # Fight Pass's UI search hits a separate 'vesper' search service. It
    # surfaces per-fight VODs that aren't in any walkable playlist — e.g.
    # pre-2023 standalone fight VODs like vod 53279 'Aiemann Zahabi vs
    # Reginaldo Vieira UFC Fight Night'. For each fighter who has at least
    # one fight without a per-fight URL, search vesper by last name, parse
    # the returned VODs through the existing matcher.
    if search_vesper:
        print(f'\n=== PASS 4: vesper search by fighter name ===')
        # Find fighters whose fights are missing fightpass_url
        missing_fighters = set()
        for fi in fights:
            if not fi.get('fightpass_url'):
                if fi['fighter1_id']: missing_fighters.add(fi['fighter1_id'])
                if fi['fighter2_id']: missing_fighters.add(fi['fighter2_id'])
        # Optional org scope: only search fighters who appear in the named orgs
        # (e.g. --orgs DWCS to populate just that org's per-fight VODs fast).
        if only_orgs:
            wanted = {o.strip().lower() for o in only_orgs}
            org_fighters = set()
            for fi in fights:
                if (org_by_event.get(fi['event_id']) or '').lower() in wanted:
                    if fi['fighter1_id']: org_fighters.add(fi['fighter1_id'])
                    if fi['fighter2_id']: org_fighters.add(fi['fighter2_id'])
            missing_fighters &= org_fighters
            print(f'  scoped to orgs {sorted(wanted)}: {len(missing_fighters)} fighters')
        # Dedupe by last name (one query catches A's all fights regardless
        # of order in the VOD title).
        last_names_to_search = {}
        for f in fighters:
            if f['id'] not in missing_fighters:
                continue
            ln = surname(f['name'])
            if not ln:
                continue
            last_names_to_search.setdefault(ln, []).append(f['id'])
        print(f'  {len(last_names_to_search)} unique last names to search')

        seen_vesper = set()  # avoid re-processing the same vod twice
        for i, last in enumerate(sorted(last_names_to_search.keys())):
            if i % 100 == 0 and i:
                print(f'  ...{i}/{len(last_names_to_search)} searched')
            try:
                results = fp.vesper_search(last)
            except Exception as e:
                print(f'  [WARN] vesper search {last!r}: {e}')
                continue
            for vid, v_title in results:
                vod_id = str(vid)
                if vod_id in seen or vod_id in seen_vesper:
                    continue
                seen_vesper.add(vod_id)
                tl = v_title.lower()
                if any(kw in tl for kw in SKIP_KEYWORDS):
                    continue
                a, b, hint = parse_fight_vod_title(v_title)
                if not a or not b:
                    continue
                ids_a = resolve_fighter(a, by_name, by_alias, by_last)
                ids_b = resolve_fighter(b, by_name, by_alias, by_last)
                candidates = []
                if ids_a and ids_b:
                    cands_map = {}
                    for x in ids_a:
                        for y in ids_b:
                            for f in fight_lookup.get(frozenset((x, y)), []):
                                cands_map[f['id']] = f
                    candidates = list(cands_map.values())
                candidates = _drop_future(candidates, ev_by_id)
                picked = None
                if len(candidates) == 1:
                    picked = candidates[0]
                elif len(candidates) > 1 and hint:
                    hint_n = _norm(hint)
                    for f in candidates:
                        ev = ev_by_id.get(f['event_id'])
                        if ev and hint_n and hint_n in _norm(ev['name']):
                            picked = f; break
                    if not picked:
                        cand_events = [ev_by_id[f['event_id']] for f in candidates
                                       if f['event_id'] in ev_by_id]
                        cand_norm = {_norm(e['name']): e for e in cand_events}
                        ev_match = match_event_fuzzy(hint, cand_events, cand_norm)
                        if ev_match:
                            for f in candidates:
                                if f['event_id'] == ev_match['id']:
                                    picked = f; break
                # Fallback: exact resolution missed (FP name spelling/nickname).
                # Pin the event from the hint, fuzzy-match the fight within it.
                if not picked and not candidates and hint:
                    picked = event_anchored_fight(a, b, hint, events,
                                                  all_events_by_norm,
                                                  fights_by_event, fighter_names)
                if picked:
                    ev = ev_by_id.get(picked['event_id'])
                    url = WATCH_URL.format(vod_id)
                    label = f'{a} vs {b}  @ {ev["name"] if ev else "?"} [vesper]'
                    matched_fights.append((picked['id'], url, label,
                                       'fight_alt' if is_alt_broadcast(v_title) else 'fight'))
                    matched_vod_ids.append(vod_id)
            time.sleep(0.1)
        print(f'  vesper pass complete — {len(seen_vesper)} unique VODs evaluated')

    # ── Dedupe per-event by variant (keep first per (event,variant)) ──────────
    event_main    = {}
    event_prelims = {}
    for ev_id, variant, url, ep_title in matched_events:
        store = event_main if variant == 'main' else event_prelims
        store.setdefault(ev_id, (url, ep_title))

    # Priority: standard per-fight VOD > event-level > SnoopCast (alt broadcast).
    # SnoopCast ('fight_alt') is used only when no standard video exists.
    _SRC_PRIO = {'fight': 3, 'event': 2, 'fight_alt': 1}
    fight_url = {}
    for fid, url, label, source in matched_fights:
        cur = fight_url.get(fid)
        if cur is None or _SRC_PRIO.get(source, 0) > _SRC_PRIO.get(cur[2], 0):
            fight_url[fid] = (url, label, source)
    perfight_count = sum(1 for v in fight_url.values() if v[2] in ('fight', 'fight_alt'))
    eventlvl_count = sum(1 for v in fight_url.values() if v[2] == 'event')

    # ── Write review file ─────────────────────────────────────────────────────
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as out:
        out.write('UFC FIGHT PASS MATCH RESULTS\n')
        out.write(f'Events  — main: {len(event_main)}  prelims: {len(event_prelims)}\n')
        out.write(f'Fights  — matched: {len(fight_url)} '
                  f'(per-fight URL: {perfight_count}, event URL fallback: {eventlvl_count})\n')
        out.write(f'Unmatched — episodes: {len(unmatched_eps)}  '
                  f'sub-events: {len(unmatched_subs)}  '
                  f'per-fight VODs: {len(per_fight_unmatched)}\n')
        out.write('=' * 70 + '\n\n')

        out.write(f'MATCHED EVENTS — MAIN ({len(event_main)})\n' + '-' * 70 + '\n')
        for ev_id, (url, t) in event_main.items():
            out.write(f"event:    {ev_by_id[ev_id]['name']}\n")
            out.write(f"episode:  {t}\n")
            out.write(f"url:      {url}\n\n")

        out.write(f'\nMATCHED EVENTS — PRELIMS ({len(event_prelims)})\n' + '-' * 70 + '\n')
        for ev_id, (url, t) in event_prelims.items():
            out.write(f"event:    {ev_by_id[ev_id]['name']}\n")
            out.write(f"episode:  {t}\n")
            out.write(f"url:      {url}\n\n")

        out.write(f'\nMATCHED FIGHTS ({len(fight_url)})\n' + '-' * 70 + '\n')
        for fid, (url, label, source) in fight_url.items():
            out.write(f"fight_id: {fid}  [{source}]\n  {label}\n  {url}\n\n")

        out.write(f'\nUNMATCHED EPISODES ({len(unmatched_eps)})\n' + '-' * 70 + '\n')
        for vid, t, u in unmatched_eps:
            out.write(f"  vod={vid}  {t!r}\n")

        out.write(f'\nUNMATCHED SUB-EVENTS ({len(unmatched_subs)})\n' + '-' * 70 + '\n')
        for vid, ev_name, matchup in unmatched_subs:
            out.write(f"  vod={vid}  {ev_name}  | {matchup}\n")

        out.write(f'\nUNMATCHED PER-FIGHT VODs ({len(per_fight_unmatched)})\n' + '-' * 70 + '\n')
        for vid, t, reason in per_fight_unmatched:
            out.write(f"  vod={vid}  {t!r}  — {reason}\n")

    print(f'\nEvents — main: {len(event_main)}  prelims: {len(event_prelims)}')
    print(f'Fights — matched: {len(fight_url)} '
          f'(per-fight URL: {perfight_count}, event URL fallback: {eventlvl_count})')
    print(f'Unmatched — episodes: {len(unmatched_eps)}  '
          f'sub-events: {len(unmatched_subs)}  '
          f'per-fight VODs: {len(per_fight_unmatched)}')
    print(f'Saved to: {OUTPUT_FILE}')

    if push:
        print(f'\nPushing event URLs ({len(event_main) + len(event_prelims)})...')
        ok = 0
        for ev_id, (url, _) in event_main.items():
            if update_event(ev_id, {'fightpass_url': url}):
                ok += 1
        for ev_id, (url, _) in event_prelims.items():
            if update_event(ev_id, {'fightpass_prelims_url': url}):
                ok += 1
        print(f'  {ok} event-level URLs updated')

        print(f'Pushing fight URLs ({len(fight_url)})...')
        ok = sum(1 for fid, (url, _, _) in fight_url.items()
                 if update_fight(fid, {'fightpass_url': url}))
        print(f'  {ok} fight-level URLs updated')

        # Mark vodIds seen so subsequent runs skip them.
        for vid in matched_vod_ids:
            mark_seen(vid)
    else:
        print('\nDry run — pass --push to write URLs')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--push', action='store_true', help='Write URLs to DB')
    parser.add_argument('--force', action='store_true',
                        help='Re-scrape episodes already in fightpass_seen_episodes.txt')
    parser.add_argument('--scan-vods', action='store_true',
                        help='Brute-force scan VOD-IDs in VOD_SCAN_START..VOD_SCAN_END '
                             'to find standalone fight VODs not in any playlist (slow)')
    parser.add_argument('--scan-start', type=int, default=None,
                        help='Override VOD scan start ID')
    parser.add_argument('--scan-end',   type=int, default=None,
                        help='Override VOD scan end ID')
    parser.add_argument('--search-vesper', action='store_true',
                        help="PASS 4: search Fight Pass's vesper service by "
                             'fighter last name for missing per-fight VOD URLs')
    parser.add_argument('--orgs', type=str, default=None,
                        help='Scope the vesper pass to fighters in these orgs '
                             '(comma-separated), e.g. --orgs "DWCS"')
    args = parser.parse_args()
    main(push=args.push, force=args.force, scan_vods=args.scan_vods,
         scan_start=args.scan_start, scan_end=args.scan_end,
         search_vesper=args.search_vesper,
         only_orgs=args.orgs.split(',') if args.orgs else None)
