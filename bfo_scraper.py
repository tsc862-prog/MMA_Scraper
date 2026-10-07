#!/usr/bin/env python3
"""
bfo_scraper.py — betting odds from BestFightOdds (bestfightodds.com) into
`fight_odds`: each fighter's opening line and closing range for every bout
BFO has tracked since 2007 (UFC, Bellator, PFL, ONE, RIZIN, KSW, ACA, …).

Why the fighter pages, not the event pages. An event page only shows what
each sportsbook is quoting *now* — on a 2007 card every cell is empty. The
fighter page (/fighters/<slug>-<id>) is the archive: one row pair per bout
with "Open", a "Closing range" (lowest … highest closing line across the
books) and the opponent's side, plus the event link and date. Both sides of
a bout sit on either fighter's page, so one page per bout is enough. The
site's robots.txt allows everything; its chart JSON (/api/ggd) is obfuscated
and left alone.

Odds are American (-210 favourite / +175 underdog). `fighterN_close` is the
midpoint of the closing range, averaged in decimal odds and converted back —
the one number the app shows. Sides are stored in the DB's fighter1 /
fighter2 order. For an upcoming bout the "closing" columns hold the current
line until the nightly refreshes it after the card.

What a nightly run does (no tracker file — state lives in the DB):
  1. sitemap-events.xml → BFO events whose date is within ±--days (10) of
     today: cards that just happened (lines are now final) and the next
     ones (current lines, refreshed nightly). Non-MMA cards (BJJ, Power
     Slap, bare-knuckle, boxing…) are skipped by slug.
  2. Each event page → its matchups (BFO matchup id + two fighter slugs).
  3. One fighter page per matchup (cached per run) → that bout's open /
     closing range for both fighters. A bout with no line movement yet has
     no matchup id on the fighter page, so rows are found by matchup id or
     by (event, opponent).
  4. Match to the DB: candidate events within ±3 days of BFO's date, fighter
     names resolved exact → alias → accent-folded → surname inside that
     small pool (verdict_scraper's Pool). Upsert (fight_id, 'bfo').
  Unmatched bouts go to bfo_unmatched.txt (not committed). BFO covers many
  orgs the DB doesn't, so a bout whose fighters are unknown to the DB
  altogether is only counted (nightly: both unknown; sweep: either).

Back-catalog: `--sweep` walks every fighter in sitemap-teams.xml (~22k),
skipping pages whose fighter isn't in the DB (their bouts couldn't match
anyway; --all-fighters fetches them too). One page a second — run it once
in the background; `--resume` continues from bfo_sweep_progress.txt. Bouts
already stored are skipped unless --force.

  python bfo_scraper.py                       # nightly: cards within ±10 days
  python bfo_scraper.py --dry-run             # parse + match, write nothing
  python bfo_scraper.py --event ufc-73-stacked-1   # one BFO event (slug or id)
  python bfo_scraper.py --fighter anderson-silva-38 # one fighter page, all bouts
  python bfo_scraper.py --sweep               # one-off back-catalog walk
  python bfo_scraper.py --sweep --resume      # continue an interrupted sweep
  python bfo_scraper.py --sweep --since 2015-01-01  # only bouts from 2015 on
"""
import os
import re
import sys
import argparse
import datetime as dt

import requests
from bs4 import BeautifulSoup

import sherdog_scraper as s        # SUPABASE creds, upsert
import verdict_scraper as v        # fetch (1 s delay + retries), fetch_all, Pool, fold, load_aliases

BASE = 'https://www.bestfightodds.com'
SOURCE = 'bfo'
UNMATCHED_FILE = 'bfo_unmatched.txt'
PROGRESS_FILE = 'bfo_sweep_progress.txt'
FUTURE_EVENT_ID = '197'            # BFO's placeholder "event" for unscheduled bouts
NON_MMA = {'bjj', 'slap', 'bare', 'knuckle', 'bkfc', 'boxing', 'kickboxing', 'glory',
           'karate', 'grappling', 'adcc', 'wrestling', 'sumo', 'muay', 'k-1', 'k1'}

# ── Odds maths ────────────────────────────────────────────────────────────────

def parse_american(text):
    """'+155' → 155, '-180' → -180, 'EV' → 100, '' → None."""
    m = re.search(r'([+-])\s*(\d+)', text or '')
    if not m:
        return 100 if (text or '').strip().upper() in ('EV', 'EVEN') else None
    val = int(m.group(2))
    return -val if m.group(1) == '-' else val

def to_decimal(a):
    return 1 + a / 100 if a > 0 else 1 + 100 / -a

def to_american(d):
    return int(round((d - 1) * 100)) if d >= 2 else -int(round(100 / (d - 1)))

def close_mid(lo, hi):
    """Midpoint of a closing range, averaged in decimal odds."""
    if lo is None or hi is None:
        return lo if hi is None else hi
    return to_american((to_decimal(lo) + to_decimal(hi)) / 2)

# ── Sitemaps ──────────────────────────────────────────────────────────────────

def _slug_id(url):
    """'/events/ufc-73-stacked-1' or a full URL → ('ufc-73-stacked-1', '1')."""
    slug = url.rstrip('/').rsplit('/', 1)[-1]
    return slug, slug.rsplit('-', 1)[-1]

def is_mma_slug(slug):
    return not (set(slug.split('-')) & NON_MMA)

def sitemap_events():
    """[(slug, id, date)] from sitemap-events.xml — lastmod is the event date."""
    xml = v.fetch(f'{BASE}/sitemap-events.xml')
    out = []
    for m in re.finditer(r'<loc>([^<]+)</loc>\s*<lastmod>(\d{4}-\d{2}-\d{2})', xml):
        slug, eid = _slug_id(m.group(1))
        out.append((slug, eid, dt.date.fromisoformat(m.group(2))))
    return out

def sitemap_fighters():
    """Fighter slugs from sitemap-teams.xml, in sitemap order."""
    xml = v.fetch(f'{BASE}/sitemap-teams.xml')
    return [_slug_id(m.group(1))[0] for m in re.finditer(r'<loc>([^<]+/fighters/[^<]+)</loc>', xml)]

def slug_name(slug):
    """'anderson-silva-38' → 'anderson silva'."""
    return slug.rsplit('-', 1)[0].replace('-', ' ')

# ── Parsing ───────────────────────────────────────────────────────────────────

def parse_event_page(html):
    """→ {'name','date','matchups': [{'id','slugs': [slug1, slug2], 'names': [..]}]}
    or None. Matchups come from the wide odds table: two rows per bout, each
    with a chart button whose data-li is [side, matchup id]; prop rows (class
    'pr') are skipped."""
    soup = BeautifulSoup(html, 'html.parser')
    ld = soup.find('script', type='application/ld+json')
    name = date = None
    if ld:
        m = re.search(r'"name":\s*"([^"]+)"', ld.get_text())
        name = m.group(1) if m else None
        m = re.search(r'"startDate":\s*"(\d{4}-\d{2}-\d{2})', ld.get_text())
        date = dt.date.fromisoformat(m.group(1)) if m else None
    tables = soup.select('table.odds-table')
    if not tables:
        return None
    bouts = {}
    for tr in tables[-1].find_all('tr'):
        if 'pr' in (tr.get('class') or []):
            continue
        btn = tr.select_one('td.but-si[data-li]')
        link = tr.select_one('th a[href^="/fighters/"]')
        if not btn or not link:
            continue
        m = re.match(r'\[(\d),\s*(\d+)\]', btn['data-li'])
        if not m:
            continue
        side, mu = int(m.group(1)), m.group(2)
        b = bouts.setdefault(mu, {'id': mu, 'slugs': [None, None], 'names': [None, None]})
        b['slugs'][side - 1] = _slug_id(link['href'])[0]
        b['names'][side - 1] = link.get_text(strip=True)
    return {'name': name, 'date': date,
            'matchups': [b for b in bouts.values() if all(b['slugs'])]}

_ORDINAL = re.compile(r'(\d+)(st|nd|rd|th)')

def parse_bfo_date(text):
    """'Oct 31st 2020' / 'Sept 4th 2021' → date, or None."""
    t = _ORDINAL.sub(r'\1', text or '').strip()
    t = re.sub(r'^Sept ', 'Sep ', t)
    try:
        return dt.datetime.strptime(t, '%b %d %Y').date()
    except ValueError:
        return None

def _side(tr):
    link = tr.select_one('th a[href^="/fighters/"]')
    lines = [parse_american(sp.get_text()) for sp in tr.select('td.moneyline span')]
    lines += [None] * (3 - len(lines))
    return {'name': link.get_text(strip=True) if link else None,
            'slug': _slug_id(link['href'])[0] if link else None,
            'open': lines[0], 'close_low': lines[1], 'close_high': lines[2]}

def parse_fighter_page(html):
    """→ [{'key','matchup','event_slug','event_id','event_name','date','a','b'}]
    where a is the page's fighter and b the opponent, each
    {'name','slug','open','close_low','close_high'}. `matchup` is BFO's id
    (None when the bout has no line history yet); `key` is the matchup id or
    '<event id>/<slug>/<slug>'. Stops at the "Rumored & future matchups"
    header."""
    soup = BeautifulSoup(html, 'html.parser')
    table = soup.select_one('table.team-stats-table')
    if not table:
        return []
    rows = table.find('tbody').find_all('tr', recursive=False)
    bouts, i = [], 0
    while i < len(rows):
        tr = rows[i]
        cls = tr.get('class') or []
        if 'rumored-header' in cls:
            break
        if 'main-row' not in cls:
            i += 1
            continue
        opp = rows[i + 1] if i + 1 < len(rows) else None
        i += 2
        if opp is None or opp.get('class'):
            continue
        chart = tr.select_one('[data-li]')
        m = re.match(r'\[(\d+),\s*(\d)\]', chart['data-li']) if chart else None
        ev = tr.select_one('a[href^="/events/"]')
        date_td = opp.select_one('td.item-non-mobile')
        if not (ev and date_td):
            continue
        slug, eid = _slug_id(ev['href'])
        if eid == FUTURE_EVENT_ID:
            continue
        date = parse_bfo_date(date_td.get_text(strip=True))
        a, b = _side(tr), _side(opp)
        if not date or not a['slug'] or not b['slug']:
            continue
        mu = m.group(1) if m else None
        bouts.append({'key': mu or f'{eid}/' + '/'.join(sorted((a['slug'], b['slug']))),
                      'matchup': mu, 'event_slug': slug, 'event_id': eid,
                      'event_name': ev.get_text(strip=True), 'date': date, 'a': a, 'b': b})
    return bouts

# ── Matching + rows ───────────────────────────────────────────────────────────

class Pools:
    """verdict_scraper.Pool per date, cached (a fighter page spans 20 years).
    The nightly touches a few dates, so each pool is fetched from the DB; the
    sweep touches thousands, so `preload()` pulls every event, fight and
    fighter once (~110 requests) and builds the pools in memory."""
    def __init__(self, aliases):
        self.aliases, self.cache, self.local = aliases, {}, None

    def preload(self, fighters):
        events = v.fetch_all('events', 'id,name,date')
        fights = v.fetch_all('fights', 'id,event_id,fighter1_id,fighter2_id')
        by_event = {}
        for f in fights:
            by_event.setdefault(f['event_id'], []).append(f)
        self.local = {'events': events, 'by_event': by_event,
                      'names': {f['id']: f['name'] for f in fighters}}

    def get(self, date):
        if date not in self.cache:
            self.cache[date] = self._local(date) if self.local else v.Pool(date, self.aliases)
        return self.cache[date]

    def _local(self, date):
        """A verdict_scraper.Pool built from the preloaded tables."""
        lo, hi = str(date - dt.timedelta(days=v.DATE_SLACK)), str(date + dt.timedelta(days=v.DATE_SLACK))
        p = v.Pool.__new__(v.Pool)
        p.events = [e for e in self.local['events'] if e['date'] and lo <= e['date'] <= hi]
        p.fights = [f for e in p.events for f in self.local['by_event'].get(e['id'], [])]
        fids = {f['fighter1_id'] for f in p.fights} | {f['fighter2_id'] for f in p.fights}
        fids.discard(None)
        p.names = {i: self.local['names'][i] for i in fids if i in self.local['names']}
        p.by_exact, p.by_fold, p.by_last = {}, {}, {}
        for fid, nm in p.names.items():
            p.by_exact.setdefault(nm.lower().strip(), set()).add(fid)
            p.by_fold.setdefault(v.fold(nm), set()).add(fid)
            p.by_last.setdefault(v.surname(nm), set()).add(fid)
        p.aliases = self.aliases
        p.pairs = {}
        for f in p.fights:
            if f['fighter1_id'] and f['fighter2_id']:
                p.pairs.setdefault(frozenset((f['fighter1_id'], f['fighter2_id'])), []).append(f)
        return p

class Known:
    """Every fighter name in the DB (exact, accent-folded, token-flipped) plus
    the alias table — a cheap 'could this name be in the DB at all?' so bouts
    from orgs we don't track are counted, not listed, and the sweep can skip
    fighters that aren't ours."""
    def __init__(self, aliases, fighters):
        self.names = set(aliases)
        for f in fighters:
            n = v.fold(f['name'])
            self.names.add(n)
            self.names.add(' '.join(reversed(n.split())))

    def __contains__(self, name):
        n = v.fold(name)
        return n in self.names or ' '.join(reversed(n.split())) in self.names

def _cols(prefix, side):
    return {f'{prefix}_open': side['open'], f'{prefix}_close_low': side['close_low'],
            f'{prefix}_close_high': side['close_high'],
            f'{prefix}_close': close_mid(side['close_low'], side['close_high'])}

def bout_row(bout, pools, now):
    """→ (row, reason). row is None when unmatched."""
    pool = pools.get(bout['date'])
    if not pool.events:
        return None, 'no DB event'
    fight, why = pool.find_fight(bout['a']['name'], bout['b']['name'])
    if not fight:
        return None, why
    a_first = fight['fighter1_id'] in pool.resolve(bout['a']['name'])
    f1, f2 = (bout['a'], bout['b']) if a_first else (bout['b'], bout['a'])
    if all(x is None for x in (f1['open'], f1['close_low'], f2['open'], f2['close_low'])):
        return None, 'no lines'
    row = {'fight_id': fight['id'], 'source': SOURCE, 'source_key': bout['key'],
           'source_url': f'{BASE}/events/{bout["event_slug"]}', 'scraped_at': now}
    row.update(_cols('fighter1', f1))
    row.update(_cols('fighter2', f2))
    return row, 'matched'

def stored_keys():
    """source_keys already in fight_odds."""
    rows = v.fetch_all('fight_odds', 'source_key', f'&source=eq.{SOURCE}', order='fight_id')
    return {r['source_key'] for r in rows if r['source_key']}

class Run:
    def __init__(self, args):
        self.args = args
        aliases = v.load_aliases()
        fighters = v.fetch_all('fighters', 'id,name')
        self.pools = Pools(aliases)
        if args.sweep or args.fighter:           # thousands of dates: build pools in memory
            self.pools.preload(fighters)
        self.known = Known(aliases, fighters)
        self.pages = {}                     # fighter slug → parsed bouts
        self.done = set()                   # bout keys handled this run
        self.skip = set() if (args.force or args.dry_run) else stored_keys()
        self.unmatched, self.rows = [], []
        self.n_matched = self.n_seen = self.n_foreign = self.n_nolines = self.n_skipped_pages = 0
        self.since = dt.date.fromisoformat(args.since) if args.since else None

    def fighter_bouts(self, slug):
        if slug not in self.pages:
            html = v.fetch(f'{BASE}/fighters/{slug}')
            self.pages[slug] = parse_fighter_page(html) if html else []
        return self.pages[slug]

    def take(self, bout, strict=False):
        """Match + queue one bout. Returns True when a row was queued. A bout
        whose fighters the DB doesn't know (both, or either when `strict`)
        is counted as foreign instead of listed."""
        key = bout['key']
        if key in self.done or key in self.skip:
            return False
        if self.since and bout['date'] < self.since:
            return False
        self.done.add(key)
        self.n_seen += 1
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        row, why = bout_row(bout, self.pools, now)
        if row:
            self.rows.append(row)
            self.n_matched += 1
            return True
        if why == 'no lines':                 # the fight is ours, BFO just has no numbers
            self.n_nolines += 1
            return False
        known = [bout['a']['name'] in self.known, bout['b']['name'] in self.known]
        # strict (sweep): a name the DB knows but that card's pool lacks means the
        # DB doesn't carry that card (a UFC vet on an Oktagon show) — not an alias gap
        if why == 'no DB event' or not any(known) or (strict and (not all(known) or why.startswith('fighter not found'))):
            self.n_foreign += 1
        else:
            self.unmatched.append(f'{bout["date"]} {bout["event_name"]} [{bout["event_slug"]}]: '
                                  f'{bout["a"]["name"]} vs {bout["b"]["name"]} — {why}')
        return False

    def flush(self):
        if self.rows and not self.args.dry_run:
            for i in range(0, len(self.rows), 500):
                s.upsert('fight_odds', self.rows[i:i + 500])
        self.rows = []

    # ── modes ──
    def event(self, slug, eid):
        html = v.fetch(f'{BASE}/events/{slug}')
        ev = parse_event_page(html) if html else None
        if not ev:
            print(f'  [{slug}] not an event page')
            return
        wanted = [m for m in ev['matchups'] if m['id'] not in self.skip and m['id'] not in self.done]
        got = 0
        for m in wanted:
            found = None
            for own, other in (m['slugs'], m['slugs'][::-1]):   # second page only if the first lacks it
                for b in self.fighter_bouts(own):
                    if b['matchup'] == m['id'] or (b['event_id'] == eid and b['b']['slug'] == other):
                        found = b
                        break
                if found:
                    break
            if found:
                if found['matchup'] is None:          # key by BFO's matchup id now that we have it
                    found['key'] = found['matchup'] = m['id']
                got += self.take(found)
            else:
                self.unmatched.append(f'{ev["date"]} {ev["name"]} [{slug}]: '
                                      f'{m["names"][0]} vs {m["names"][1]} — not on either fighter page')
        print(f'  [{slug}] {ev["name"]} ({ev["date"]}): {got}/{len(wanted)} bouts matched'
              + (f', {len(ev["matchups"]) - len(wanted)} already stored' if len(wanted) < len(ev['matchups']) else ''))
        self.flush()

    def fighter(self, slug, quiet=False, strict=False):
        bouts = self.fighter_bouts(slug)
        got = sum(self.take(b, strict=strict) for b in bouts)
        if not quiet:
            print(f'  [{slug}] {got}/{len(bouts)} bouts matched')
        self.flush()
        return got, len(bouts)

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--dry-run', action='store_true', help='parse + match, write nothing')
    ap.add_argument('--event', help='one BFO event slug or id')
    ap.add_argument('--fighter', help='one BFO fighter slug: store every bout on the page')
    ap.add_argument('--sweep', action='store_true', help='walk every fighter in the sitemap (one-off back-catalog)')
    ap.add_argument('--resume', action='store_true', help=f'sweep: continue from {PROGRESS_FILE}')
    ap.add_argument('--all-fighters', action='store_true', help='sweep: fetch pages of fighters the DB does not know too')
    ap.add_argument('--since', help='skip bouts dated before this (YYYY-MM-DD)')
    ap.add_argument('--days', type=int, default=10, help='nightly: BFO events dated within ±N days of today (default 10)')
    ap.add_argument('--force', action='store_true', help='re-store bouts that already have odds rows')
    ap.add_argument('--limit', type=int, default=0, help='sweep: stop after N fighter pages (resume later)')
    args = ap.parse_args()
    if not s.SUPABASE_URL or not s.SUPABASE_KEY:
        sys.exit('Missing SUPABASE creds in .env')

    run = Run(args)
    try:
        if args.event:
            slug = args.event
            if slug.isdigit():
                slug = next((sl for sl, eid, _ in sitemap_events() if eid == slug), None)
                if not slug:
                    sys.exit(f'BFO event id {args.event} not in the sitemap')
            run.event(slug, _slug_id(slug)[1])
        elif args.fighter:
            run.fighter(args.fighter)
        elif args.sweep:
            slugs = sitemap_fighters()
            start = 0
            if args.resume and os.path.exists(PROGRESS_FILE):
                last = open(PROGRESS_FILE, encoding='utf-8').read().strip()
                if last in slugs:
                    start = slugs.index(last) + 1
            end = min(len(slugs), start + args.limit) if args.limit else len(slugs)
            print(f'Sweeping fighters {start}..{end - 1} of {len(slugs)}'
                  + (f' (bouts from {args.since} on)' if args.since else ''))
            for i in range(start, end):
                if args.all_fighters or slug_name(slugs[i]) in run.known:
                    try:
                        run.fighter(slugs[i], quiet=True, strict=True)
                    except requests.RequestException as e:
                        print(f'  [{slugs[i]}] request failed, skipped: {e}')
                        continue
                else:
                    run.n_skipped_pages += 1
                if (i + 1) % 500 == 0:
                    print(f'  {i + 1}/{len(slugs)}: {run.n_matched} matched, {run.n_skipped_pages} pages skipped')
                with open(PROGRESS_FILE, 'w', encoding='utf-8') as f:
                    f.write(slugs[i] + '\n')
        else:
            today = dt.date.today()
            lo, hi = today - dt.timedelta(days=args.days), today + dt.timedelta(days=args.days)
            events = [(sl, eid) for sl, eid, d in sitemap_events()
                      if lo <= d <= hi and eid != FUTURE_EVENT_ID and is_mma_slug(sl)]
            print(f'{len(events)} BFO events dated {lo}..{hi}')
            run.skip = set()                    # refresh everything in the window: lines move until the bell
            for slug, eid in events:
                try:
                    run.event(slug, eid)
                except requests.RequestException as e:
                    print(f'  [{slug}] request failed, skipped: {e}')
    finally:
        run.flush()

    print(f'\n{run.n_matched}/{run.n_seen} bouts matched, {len(run.unmatched)} unmatched, '
          f'{run.n_foreign} on cards the DB does not carry, {run.n_nolines} matched but without lines'
          + (f', {run.n_skipped_pages} fighter pages skipped' if run.n_skipped_pages else '')
          + (' (dry run — nothing written)' if args.dry_run else ''))
    if run.unmatched:
        with open(UNMATCHED_FILE, 'w', encoding='utf-8') as f:
            f.write('\n'.join(run.unmatched) + '\n')
        print(f'Unmatched bouts written to {UNMATCHED_FILE}')
    elif os.path.exists(UNMATCHED_FILE):
        os.remove(UNMATCHED_FILE)

if __name__ == '__main__':
    main()
