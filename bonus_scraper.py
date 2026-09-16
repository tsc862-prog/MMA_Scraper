#!/usr/bin/env python3
"""
bonus_scraper.py — UFC post-fight bonus awards from Wikipedia into
`fight_bonuses`: Fight of the Night (FOTN), Performance of the Night (POTN)
and the pre-2014 Knockout / Submission of the Night (KOTN / SOTN).

An objective, back-catalog-wide "the crowd liked this one" signal: the app
gives a bonus fight a small fixed bump in its blended score, and it covers
every UFC card since the bonuses began (2005), where crowd ratings only start
in 2025.

Source. Wikipedia's "List of UFC events" past-events table gives every card's
name, date and article link; each article has a "Bonus awards" section, a
bulleted list like
    Fight of the Night: Justin Gaethje vs. Paddy Pimblett
    Performance of the Night: Josh Hokit and Ty Miller
Events are matched to the DB (organization UFC) by date, with a headliner-
surname check when two UFC cards share a date. FOTN resolves both names to a
bout on that card; the per-fighter awards resolve the fighter and take their
bout that night. Names resolve exact → alias → accent-folded → surname within
that one card. Pages are fetched one per second with a descriptive User-Agent
as Wikipedia asks. No tracker file: the nightly re-checks UFC events from the
last --days (45) that have no bonus rows yet — Wikipedia usually has the
awards within a day of the card — so the cost is a handful of pages a night.

  python bonus_scraper.py                 # nightly: recent UFC events lacking bonuses
  python bonus_scraper.py --all           # one-off backfill of every UFC event
  python bonus_scraper.py --all --force   # ...including events that already have rows
  python bonus_scraper.py --dry-run       # parse + match, write nothing
Unmatched awards go to bonus_unmatched.txt (not committed).
"""
import os
import re
import sys
import time
import html as htmlmod
import argparse
import datetime as dt

import requests
from bs4 import BeautifulSoup

import sherdog_scraper as s
from verdict_scraper import fetch_all, fold, surname, resolve_name

LIST_URL = 'https://en.wikipedia.org/wiki/List_of_UFC_events'
UA = 'RewatchMMA-scraper/1.0 (personal fight-rewatch app; contact tsc862@gmail.com)'
UFC_ORG_ID = 2
UNMATCHED_FILE = 'bonus_unmatched.txt'
DELAY = 1.0
AWARDS = {
    'fight of the night': 'FOTN',
    'performance of the night': 'POTN',
    'knockout of the night': 'KOTN',
    'submission of the night': 'SOTN',
}

def fetch(url):
    r = requests.get(url, headers={'User-Agent': UA}, timeout=60)
    time.sleep(DELAY)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.text

# ── Wikipedia ─────────────────────────────────────────────────────────────────

_MONTHS = '(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*'

def list_events(html):
    """Past-events table → [{'name','date','url'}], newest first."""
    soup = BeautifulSoup(html, 'html.parser')
    out = []
    for tbl in soup.find_all('table'):
        if 'Past_events' not in (tbl.get('data-x-id') or '') and 'Event' not in tbl.get_text()[:400]:
            continue
        for tr in tbl.find_all('tr'):
            tds = tr.find_all('td')
            if len(tds) < 3:
                continue
            a = tds[1].find('a', href=True)
            if not a:
                continue
            date = None
            sv = tds[2].find(attrs={'data-sort-value': True})
            if sv:
                m = re.search(r'(\d{4})-(\d{2})-(\d{2})', sv['data-sort-value'])
                if m:
                    date = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if not date:
                m = re.search(rf'({_MONTHS}) (\d{{1,2}}), (\d{{4}})', tds[2].get_text(' ', strip=True))
                if m:
                    try:
                        date = dt.datetime.strptime(f'{m.group(1)[:3]} {m.group(2)} {m.group(3)}', '%b %d %Y').date()
                    except ValueError:
                        pass
            if not date:
                continue
            href = a['href']
            if href.startswith('/'):
                href = 'https://en.wikipedia.org' + href
            out.append({'name': a.get_text(strip=True), 'date': date, 'url': href})
        if out:
            break
    return out

def parse_bonuses(html):
    """'Bonus awards' section → [(award_code, [names...])]. A FOTN entry's
    names list holds 'A vs. B' strings; per-fighter awards hold fighter names."""
    soup = BeautifulSoup(html, 'html.parser')
    h = soup.find(id='Bonus_awards') or soup.find(id='Bonus_Awards')
    if not h:
        return []
    out = []
    node = h
    while node and (node.name != 'h2' or node is h):
        node = node.find_next(['h2', 'ul'])
        if node is None or node.name == 'h2':
            break
        for li in node.find_all('li'):
            txt = htmlmod.unescape(re.sub(r'\[\d+\]', '', li.get_text(' ', strip=True)))
            m = re.match(r'\s*([A-Za-z ]+?of the Night)\s*:\s*(.+)', txt)
            if not m:
                continue
            code = AWARDS.get(m.group(1).strip().lower())
            if not code:
                continue
            body = m.group(2).strip().rstrip('.')
            body = re.sub(r'\s*\(.*?\)', '', body)          # "(replaced …)", "($50,000)"
            body = re.sub(r'\s*\[.*?\]', '', body)          # "[a]" footnote markers
            body = body.replace('*', '')                    # "Rony Jason*"
            body = re.sub(r'\s+\d$', '', body)              # "Vera vs. Font 1" rematch numbering
            if code == 'FOTN':
                parts = re.split(r'\s+and\s+(?=[A-Z][^,]+\s+vs\.?\s)', body)
                out.append((code, [p.strip() for p in parts if 'vs' in p.lower()]))
            else:
                # "A, B, and C" / "A and B" / "A & B"; a leading "and" left by an
                # Oxford comma is dropped; "No bonus awarded" isn't a name
                parts = re.split(r',\s*(?:and\s+)?|\s+and\s+|\s*&\s*', body)
                names = [re.sub(r'^and\s+', '', p.strip()) for p in parts]
                out.append((code, [p for p in names if p and not re.match(r'no(ne)?\s+(bonus|award)', p, re.I)]))
    return out

# ── DB matching ───────────────────────────────────────────────────────────────

class Card:
    """One DB event's fights + fighters, with name resolution."""
    def __init__(self, event, aliases):
        self.event = event
        self.fights = fetch_all('fights', 'id,fighter1_id,fighter2_id', f'&event_id=eq.{event["id"]}')
        fids = {f['fighter1_id'] for f in self.fights} | {f['fighter2_id'] for f in self.fights}
        fids.discard(None)
        self.names = {}
        if fids:
            for f in fetch_all('fighters', 'id,name', f'&id=in.({",".join(map(str, fids))})'):
                self.names[f['id']] = f['name']
        self.by_fold, self.by_last = {}, {}
        for fid, nm in self.names.items():
            self.by_fold.setdefault(fold(nm), set()).add(fid)
            self.by_last.setdefault(surname(nm), set()).add(fid)
        self.aliases = aliases
        self.by_fighter = {}
        for f in self.fights:
            for k in ('fighter1_id', 'fighter2_id'):
                if f[k]:
                    self.by_fighter.setdefault(f[k], []).append(f)

    def resolve(self, name):
        return resolve_name(name, {}, self.by_fold, self.by_last, self.aliases, self.names)

    def fight_of(self, name):
        ids = self.resolve(name)
        fights = [f for i in ids for f in self.by_fighter.get(i, [])]
        return (fights[0], ids.pop()) if len(fights) == 1 else (None, None)

    def fight_between(self, a, b):
        ia, ib = self.resolve(a), self.resolve(b)
        cands = [f for f in self.fights
                 if {f['fighter1_id'], f['fighter2_id']} & ia and {f['fighter1_id'], f['fighter2_id']} & ib
                 and f['fighter1_id'] != f['fighter2_id']]
        return cands[0] if len(cands) == 1 else None

def _sig(name):
    """Surname-ish tokens of an event name for same-day disambiguation."""
    return {t for t in re.split(r'[^a-z]+', fold(name)) if len(t) > 3
            and t not in {'ufc', 'fight', 'night', 'ultimate', 'fighting', 'championship', 'live', 'espn'}}

def match_event(wp, by_date):
    cands = by_date.get(wp['date'], [])
    if len(cands) == 1:
        return cands[0]
    if not cands:
        return None
    sig = _sig(wp['name'])
    scored = sorted(cands, key=lambda e: -len(sig & _sig(e['name'])))
    return scored[0] if len(sig & _sig(scored[0]['name'])) > len(sig & _sig(scored[1]['name'])) else None

# ── Runs ──────────────────────────────────────────────────────────────────────

def process(wp, db_event, aliases, dry_run, unmatched):
    html = fetch(wp['url'])
    if html is None:
        return 0
    awards = parse_bonuses(html)
    if not awards:
        return 0
    card = Card(db_event, aliases)
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    rows = []
    for code, names in awards:
        for nm in names:
            if code == 'FOTN':
                m = re.split(r'\s+vs\.?\s+', nm, maxsplit=1)
                fight = card.fight_between(m[0], m[1]) if len(m) == 2 else None
                if not fight:
                    unmatched.append(f'{wp["date"]} {wp["name"]}: FOTN "{nm}" — no bout')
                    continue
                rows.append({'fight_id': fight['id'], 'award': code, 'fighter_id': None,
                             'source_url': wp['url'], 'scraped_at': now})
            else:
                fight, fid = card.fight_of(nm)
                if not fight:
                    unmatched.append(f'{wp["date"]} {wp["name"]}: {code} "{nm}" — fighter/bout not found')
                    continue
                rows.append({'fight_id': fight['id'], 'award': code, 'fighter_id': fid,
                             'source_url': wp['url'], 'scraped_at': now})
    # The table's key is a bigserial, so a REST upsert can't merge on
    # (fight_id, award, fighter_id) — insert only what isn't there yet.
    if rows:
        ids = ','.join(f['id'] for f in card.fights)
        have = {(b['fight_id'], b['award'], b['fighter_id'])
                for b in fetch_all('fight_bonuses', 'fight_id,award,fighter_id', f'&fight_id=in.({ids})', order='id')}
        rows = [r for r in rows if (r['fight_id'], r['award'], r['fighter_id']) not in have]
    print(f'  {wp["date"]} {wp["name"]}: {len(rows)} new awards'
          + ('' if len(rows) + len(have) == sum(len(n) for _, n in awards) else f' ({sum(len(n) for _, n in awards) - len(rows) - len(have)} unmatched)'))
    if rows and not dry_run:
        s.upsert('fight_bonuses', rows)
    return len(rows)

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--all', action='store_true', help='every UFC event, not just recent ones')
    ap.add_argument('--force', action='store_true', help='with --all: re-check events that already have bonus rows')
    ap.add_argument('--days', type=int, default=45, help='nightly window: UFC events from the last N days (default 45)')
    ap.add_argument('--limit', type=int, default=0, help='stop after N pages (backfill in chunks)')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()
    if not s.SUPABASE_URL or not s.SUPABASE_KEY:
        sys.exit('Missing SUPABASE creds in .env')

    today = dt.date.today()
    extra = f'&organization_id=eq.{UFC_ORG_ID}&date=lte.{today}'
    if not args.all:
        extra += f'&date=gte.{today - dt.timedelta(days=args.days)}'
    events = fetch_all('events', 'id,name,date', extra)
    by_date = {}
    for e in events:
        by_date.setdefault(dt.date.fromisoformat(e['date']), []).append(e)

    have = set()
    if not args.force:
        # events that already have any bonus row (fight_bonuses → fights.event_id)
        for b in fetch_all('fight_bonuses', 'fight_id,fights(event_id)', order='id'):
            ev = (b.get('fights') or {}).get('event_id')
            if ev:
                have.add(ev)

    aliases = {a['alias'].lower(): a['fighter_id']
               for a in fetch_all('fighter_aliases', 'alias,fighter_id', order='alias')}
    wp_events = list_events(fetch(LIST_URL))
    print(f'{len(wp_events)} events on Wikipedia, {len(events)} UFC events in DB in scope, {len(have)} already have bonuses')

    unmatched, n_pages, n_rows = [], 0, 0
    for wp in wp_events:
        db_event = match_event(wp, by_date)
        if not db_event or db_event['id'] in have:
            continue
        n_pages += 1
        n_rows += process(wp, db_event, aliases, args.dry_run, unmatched)
        if args.limit and n_pages >= args.limit:
            print(f'--limit {args.limit} reached')
            break
    print(f'\n{n_pages} pages, {n_rows} award rows, {len(unmatched)} unmatched'
          + (' (dry run — nothing written)' if args.dry_run else ''))
    if unmatched:
        with open(UNMATCHED_FILE, 'w', encoding='utf-8') as f:
            f.write('\n'.join(unmatched) + '\n')
        print(f'Unmatched written to {UNMATCHED_FILE}')
    elif os.path.exists(UNMATCHED_FILE):
        os.remove(UNMATCHED_FILE)

if __name__ == '__main__':
    main()
