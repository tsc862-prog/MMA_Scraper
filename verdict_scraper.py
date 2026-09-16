#!/usr/bin/env python3
"""
verdict_scraper.py — community fight + event ratings from Verdict MMA
(verdictmma.com) into `fight_crowd_ratings` / `event_crowd_ratings`, the
"other users" signal the app blends in next to your own rating.

Verdict's event pages are server-rendered, so a plain GET gives every bout on
the card with its fan score: "Umar Nurmagomedov vs. Song Yadong 8.34 /10
(455 ratings)", plus an Event Rating and a pre-event Hype Rating with counts.
Only the HTML pages are used — robots.txt allows them; the site's JSON backend
(/api/) needs a login and is left alone. Scores are stored on Verdict's own
0–10 scale; the app converts.

What a run does (no tracker file — state lives in the DB):
  1. /events/recent  → the ten most recent cards (UFC, DWCS, PFL, RIZIN, ONE…).
  2. Plus every event already in event_crowd_ratings whose date is within the
     last --settle days (30): ratings keep arriving for weeks after a card, so
     those pages are re-fetched until the counts settle.
  3. Each event page is parsed and matched to the DB: candidate events are
     those dated within a day of Verdict's date; candidate fights are theirs.
     Fighter names resolve exact → alias table → accent-folded → surname,
     restricted to that small pool so a surname is safe. The DB event that
     holds the most matched bouts gets the event/hype score.
  4. Upsert (fight_id, 'verdict') / (event_id, 'verdict'). Bouts with zero
     ratings are skipped; nothing is ever deleted.
  Unmatched bouts are listed in verdict_unmatched.txt (not committed) — fix
  with add_alias.py and the next run picks them up.

Verdict has a page for every UFC card back to 2001, but fan ratings only
carry volume from ~late 2025 on, so the back-catalog is a one-off sweep of
event ids (they are NOT chronological): `--sweep 1-2000` walks the id range,
skipping pages dated before --since (default 2025-01-01) or with no rated
bouts. Run it once, in the background, with the default 1 s delay.

  python verdict_scraper.py                  # nightly: recent + settling events
  python verdict_scraper.py --dry-run        # parse + match, write nothing
  python verdict_scraper.py --event 1919     # one Verdict event id
  python verdict_scraper.py --sweep 1-2000   # one-off back-catalog walk
"""
import os
import re
import sys
import time
import argparse
import unicodedata
import datetime as dt

import requests
from bs4 import BeautifulSoup

import sherdog_scraper as s   # SUPABASE_URL/KEY, _headers, upsert, USER_AGENT, .env load

BASE = 'https://verdictmma.com'
SOURCE = 'verdict'
UNMATCHED_FILE = 'verdict_unmatched.txt'
DELAY = 1.0            # seconds between page fetches
DATE_SLACK = 3         # days either side of Verdict's date when finding the DB event
                       # (Verdict has mis-dated pages — DWCS S10 W5 sat three days early;
                       # safe because a bout must still match both names AND a fight
                       # between them inside the window)

# ── HTTP ──────────────────────────────────────────────────────────────────────

def fetch(url, attempts=3):
    """GET with a polite delay; a dropped connection (the sweep hit a
    ConnectionReset ~120 pages in) is retried with a growing pause."""
    for i in range(attempts):
        try:
            r = requests.get(url, headers={'User-Agent': s.USER_AGENT,
                                           'Accept-Language': 'en-US,en;q=0.9'}, timeout=40)
            time.sleep(DELAY)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.text
        except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError) as e:
            if i == attempts - 1:
                raise
            print(f'  retry {i + 1} after {type(e).__name__}')
            time.sleep(5 * (i + 1))

def fetch_all(table, select, extra='', order=None):
    """Page through a Supabase table."""
    PAGE, off, out = 1000, 0, []
    order = order or 'id'
    while True:
        r = requests.get(f'{s.SUPABASE_URL}/rest/v1/{table}?select={select}'
                         f'{extra}&order={order}&limit={PAGE}&offset={off}',
                         headers=s._headers(), timeout=60)
        r.raise_for_status()
        batch = r.json()
        out += batch
        if len(batch) < PAGE:
            return out
        off += PAGE

# ── Parsing ───────────────────────────────────────────────────────────────────

_RATING = re.compile(r'([\d.]+)\s*/10\s*\((\d+)\s+ratings?\)')

def recent_event_ids(html):
    """Numeric Verdict event ids linked from /events/recent, in page order."""
    ids = []
    for m in re.finditer(r'href="/event/(\d+)"', html):
        if m.group(1) not in ids:
            ids.append(m.group(1))
    return ids

def _score_block(soup, label):
    """'Event Rating' / 'Hype Rating' → (score, count) or (None, None)."""
    lab = soup.find(lambda t: t.name == 'div' and t.get_text(strip=True) == label)
    if not lab:
        return None, None
    m = _RATING.search(lab.parent.get_text(' ', strip=True))
    return (float(m.group(1)), int(m.group(2))) if m else (None, None)

def parse_event(html, vid):
    """→ {'id','name','date','rating','rating_count','hype','hype_count',
         'fights': [{'key','a','b','rating','count'}]} or None if not an event."""
    soup = BeautifulSoup(html, 'html.parser')
    h1 = soup.find('h1')
    if not h1:
        return None
    name = h1.get_text(strip=True)
    date = None
    for sp in h1.find_all_next('span', limit=6):
        try:
            date = dt.datetime.strptime(sp.get_text(strip=True), '%B %d, %Y').date()
            break
        except ValueError:
            continue
    if not date or date.year >= 2099:      # 2099 = Verdict's "unscheduled" placeholder
        return None
    rating, rcount = _score_block(soup, 'Event Rating')
    hype, hcount = _score_block(soup, 'Hype Rating')

    fights = []
    for a in soup.select('a[href^="/rate/fights/event/"]'):
        m = re.search(r'/event/(\d+)/fight/(\d+)', a['href'])
        names = [d.get_text(strip=True) for d in a.find_all('div')
                 if d.get_text(strip=True) and not d.find('div')]
        names = [n for n in names if n.lower() != 'vs.' and not _RATING.search(n)
                 and not re.fullmatch(r'[\d.]+(/10)?', n)]
        txt = a.get_text(' ', strip=True)
        sc = _RATING.search(txt)
        if not m or len(names) < 2 or not sc:
            continue
        fights.append({'key': f'{m.group(1)}/{m.group(2)}', 'a': names[0], 'b': names[1],
                       'rating': float(sc.group(1)), 'count': int(sc.group(2)),
                       'url': f'{BASE}/event/{vid}'})
    return {'id': str(vid), 'name': name, 'date': date, 'rating': rating,
            'rating_count': rcount, 'hype': hype, 'hype_count': hcount, 'fights': fights}

# ── Matching ──────────────────────────────────────────────────────────────────

def fold(name):
    n = unicodedata.normalize('NFKD', name or '')
    n = ''.join(c for c in n if not unicodedata.combining(c))
    n = n.replace('’', "'").replace('‘', "'")      # curly apostrophes (Wikipedia) → straight
    # letters NFKD leaves alone (no combining form): Wikłacz, Özkılıç, Søren, Đorđe, Straßer
    n = n.translate(str.maketrans({'ł': 'l', 'Ł': 'L', 'ı': 'i', 'ø': 'o', 'Ø': 'O', 'đ': 'd', 'Đ': 'D', 'ß': 'ss'}))
    return re.sub(r'\s+', ' ', n.lower().replace('-', ' ').replace('.', '')).strip()

def surname(name):
    toks = fold(name).split()
    while len(toks) > 1 and toks[-1] in {'jr', 'sr', 'ii', 'iii', 'iv'}:
        toks = toks[:-1]
    return toks[-1] if toks else ''

def resolve_name(name, by_exact, by_fold, by_last, aliases, known):
    """Fighter ids for a display name within a small candidate pool:
    exact → alias table → accent-folded → surname-first flip ("Song Yadong"
    → "Yadong Song") → first+last (drops middle names / nicknames) → a
    mononym token ("Sumudaerji Tibetan Eagle" → "Sumudaerji") → surname alone
    when unique. Shared by the Verdict and bonus scrapers."""
    nl = name.lower().strip()
    ids = by_exact.get(nl)
    if ids:
        return ids
    fid = aliases.get(nl) or aliases.get(fold(name))
    if fid and fid in known:
        return {fid}
    f = fold(name)
    ids = by_fold.get(f)
    if ids:
        return ids
    toks = f.split()
    if len(toks) >= 2:
        ids = by_fold.get(' '.join(reversed(toks)))
        if ids:
            return ids
    if len(toks) > 2:
        # Korean-style "Choi Doo-ho" vs Sherdog "Doo Ho Choi": family name moves end↔front
        ids = by_fold.get(' '.join(toks[1:] + toks[:1])) or by_fold.get(' '.join(toks[-1:] + toks[:-1]))
        if ids:
            return ids
        ids = by_fold.get(f'{toks[0]} {surname(name)}') or by_fold.get(f'{toks[-1]} {toks[0]}')
        if ids:
            return ids
        for t in toks:
            ids = by_fold.get(t)
            if ids:
                return ids
        ids = by_fold.get(' '.join(toks[1:]))          # "Carlos Diego Ferreira" → "Diego Ferreira"
        if ids:
            return ids
    # the display name is a prefix of the stored one ("Ariane Lipski" → "Ariane Lipski da Silva")
    pref = {i for k, v in by_fold.items() if k.startswith(f + ' ') for i in v}
    if len(pref) == 1:
        return pref
    # ...or its first + last tokens ("Chequina Pedro" → "Chequina Noso Pedro")
    if len(toks) == 2:
        fl = {i for k, v in by_fold.items() if (kt := k.split()) and len(kt) > 2
              and kt[0] == toks[0] and kt[-1] == toks[1] for i in v}
        if len(fl) == 1:
            return fl
    ids = by_last.get(surname(name), set())
    return ids if len(ids) == 1 else set()

class Pool:
    """DB events within a day of a date, their fights, and the fighters in them."""
    def __init__(self, date, aliases):
        lo, hi = date - dt.timedelta(days=DATE_SLACK), date + dt.timedelta(days=DATE_SLACK)
        self.events = fetch_all('events', 'id,name,date',
                                f'&date=gte.{lo}&date=lte.{hi}')
        self.fights = []
        if self.events:
            ids = ','.join(str(e['id']) for e in self.events)
            self.fights = fetch_all('fights', 'id,event_id,fighter1_id,fighter2_id',
                                    f'&event_id=in.({ids})')
        fids = {f['fighter1_id'] for f in self.fights} | {f['fighter2_id'] for f in self.fights}
        fids.discard(None)
        self.names = {}
        for i in range(0, len(fids), 200):
            chunk = ','.join(str(x) for x in list(fids)[i:i + 200])
            for f in fetch_all('fighters', 'id,name', f'&id=in.({chunk})'):
                self.names[f['id']] = f['name']
        self.by_exact = {}
        self.by_fold = {}
        self.by_last = {}
        for fid, nm in self.names.items():
            self.by_exact.setdefault(nm.lower().strip(), set()).add(fid)
            self.by_fold.setdefault(fold(nm), set()).add(fid)
            self.by_last.setdefault(surname(nm), set()).add(fid)
        self.aliases = aliases   # global alias → fighter_id
        self.pairs = {}
        for f in self.fights:
            if f['fighter1_id'] and f['fighter2_id']:
                self.pairs.setdefault(frozenset((f['fighter1_id'], f['fighter2_id'])), []).append(f)

    def resolve(self, name):
        # surname-alone fallback is safe only because the pool is one or two cards
        return resolve_name(name, self.by_exact, self.by_fold, self.by_last, self.aliases, self.names)

    def find_fight(self, a, b):
        ia, ib = self.resolve(a), self.resolve(b)
        if not ia:
            return None, f'fighter not found: "{a}"'
        if not ib:
            return None, f'fighter not found: "{b}"'
        cands = []
        for x in ia:
            for y in ib:
                cands += self.pairs.get(frozenset((x, y)), [])
        if len(cands) == 1:
            return cands[0], 'matched'
        if not cands:
            return None, f'no fight for {a} vs {b}'
        return None, f'ambiguous ({len(cands)} bouts) for {a} vs {b}'

# ── Runs ──────────────────────────────────────────────────────────────────────

def load_aliases():
    return {a['alias'].lower(): a['fighter_id']
            for a in fetch_all('fighter_aliases', 'alias,fighter_id', order='alias')}

def settling_event_ids(days):
    """Verdict ids already stored whose DB event date is within `days`."""
    rows = fetch_all('event_crowd_ratings', 'event_id,source_key',
                     f'&source=eq.{SOURCE}', order='event_id')
    if not rows:
        return []
    since = dt.date.today() - dt.timedelta(days=days)
    ids = ','.join(str(r['event_id']) for r in rows)
    recent = {e['id'] for e in fetch_all('events', 'id', f'&id=in.({ids})&date=gte.{since}')}
    return [r['source_key'] for r in rows if r['event_id'] in recent and r['source_key']]

def process_event(vid, aliases, dry_run=False, unmatched=None, since=None, quiet=False):
    """Fetch + match + upsert one Verdict event. Returns (matched, total) or None."""
    html = fetch(f'{BASE}/event/{vid}')
    if html is None:
        return None
    ev = parse_event(html, vid)
    if not ev:
        return None
    if since and ev['date'] < since:
        if not quiet:
            print(f'  [{vid}] {ev["name"]} ({ev["date"]}) — before {since}, skipped')
        return None
    rated = [f for f in ev['fights'] if f['count'] > 0]
    if not rated and not ev['rating_count']:
        if not quiet:
            print(f'  [{vid}] {ev["name"]} ({ev["date"]}) — no ratings yet')
        return (0, 0)

    pool = Pool(ev['date'], aliases)
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    fight_rows, per_event = [], {}
    for f in rated:
        fight, why = pool.find_fight(f['a'], f['b'])
        if not fight:
            if unmatched is not None:
                unmatched.append(f'{ev["date"]} {ev["name"]} [{vid}]: {f["a"]} vs {f["b"]} — {why}')
            continue
        per_event[fight['event_id']] = per_event.get(fight['event_id'], 0) + 1
        fight_rows.append({'fight_id': fight['id'], 'source': SOURCE,
                           'rating': f['rating'], 'rating_count': f['count'],
                           'source_key': f['key'], 'source_url': f['url'], 'scraped_at': now})
    event_row = None
    if per_event:
        db_event = max(per_event, key=per_event.get)
        event_row = {'event_id': db_event, 'source': SOURCE,
                     'rating': ev['rating'], 'rating_count': ev['rating_count'],
                     'hype': ev['hype'], 'hype_count': ev['hype_count'],
                     'source_key': ev['id'], 'source_url': f'{BASE}/event/{vid}', 'scraped_at': now}
    print(f'  [{vid}] {ev["name"]} ({ev["date"]}): {len(fight_rows)}/{len(rated)} rated bouts matched'
          + (f', event {ev["rating"]}/10 ({ev["rating_count"]})' if ev['rating'] is not None else '')
          + ('' if per_event else ' — NO DB EVENT'))
    if not dry_run:
        s.upsert('fight_crowd_ratings', fight_rows)
        if event_row:
            s.upsert('event_crowd_ratings', event_row)
    return (len(fight_rows), len(rated))

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--dry-run', action='store_true', help='parse + match, write nothing')
    ap.add_argument('--event', help='one Verdict event id')
    ap.add_argument('--sweep', help='id range A-B: one-off back-catalog walk')
    ap.add_argument('--since', default='2025-01-01', help='sweep: skip events dated before this (YYYY-MM-DD)')
    ap.add_argument('--settle', type=int, default=30, help='re-fetch stored events dated within N days (default 30)')
    args = ap.parse_args()
    if not s.SUPABASE_URL or not s.SUPABASE_KEY:
        sys.exit('Missing SUPABASE creds in .env')

    aliases = load_aliases()
    unmatched = []
    matched = total = 0

    if args.event:
        ids, since = [args.event], None
    elif args.sweep:
        lo, hi = (int(x) for x in args.sweep.split('-'))
        ids, since = [str(i) for i in range(lo, hi + 1)], dt.date.fromisoformat(args.since)
        print(f'Sweeping Verdict event ids {lo}..{hi} (events from {since} on)')
    else:
        html = fetch(f'{BASE}/events/recent')
        recent = recent_event_ids(html)
        settling = settling_event_ids(args.settle)
        ids = recent + [i for i in settling if i not in recent]
        since = None
        print(f'{len(recent)} recent + {len(ids) - len(recent)} settling Verdict events')

    for vid in ids:
        try:
            res = process_event(vid, aliases, dry_run=args.dry_run, unmatched=unmatched,
                                since=since, quiet=bool(args.sweep))
        except requests.RequestException as e:
            print(f'  [{vid}] request failed, skipped: {e}')
            continue
        if res:
            matched += res[0]
            total += res[1]

    print(f'\n{matched}/{total} rated bouts matched, {len(unmatched)} unmatched'
          + (' (dry run — nothing written)' if args.dry_run else ''))
    if unmatched:
        with open(UNMATCHED_FILE, 'w', encoding='utf-8') as f:
            f.write('\n'.join(unmatched) + '\n')
        print(f'Unmatched bouts written to {UNMATCHED_FILE}')
    elif os.path.exists(UNMATCHED_FILE):
        os.remove(UNMATCHED_FILE)   # stale list from an earlier run

if __name__ == '__main__':
    main()
