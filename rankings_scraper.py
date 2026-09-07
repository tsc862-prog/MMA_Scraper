#!/usr/bin/env python3
"""
rankings_scraper.py — divisional + pound-for-pound rankings from ufc.com and
Sherdog into the `rankings` table, for the app's Rankings page.

Two sources, one table of dated SNAPSHOTS. Every run scrapes the current lists
and compares them with the latest stored snapshot for that source; if anything
differs (order, movement markers, records, a new champion) the lists are stored
under today's snapshot_date and the older snapshots stay put — so the table is
a history of every published change and the app can show the rankings "as of"
any date. An unchanged source writes nothing (--force overrides). A re-run on
the same day replaces that day's snapshot. A source that parses to suspiciously
few rows is skipped rather than recording a broken page as a real change. The
unique key includes fighter_name because ufc.com publishes ties (its P4P list
has carried two #10s).

  ufc      https://www.ufc.com/rankings — champion + top 15 per division and
           the two P4P lists. Names only (no ids), so each is resolved to a
           fighters.id: exact / alias / accent-folded match first, then a
           surname-first flip (ufc.com writes "Song Yadong", Sherdog "Yadong
           Song"), then a token-overlap match restricted to fighters who have
           actually fought in the UFC ("Ian Machado Garry" -> "Ian Garry",
           "Mizuki" -> "Mizuki Inoue"). Same-name collisions are broken the
           same way (three "Jean Silva"s in the DB, one with UFC bouts).
           Anything still unresolved is stored with a null fighter_id (name +
           ufc.com link only) and listed in rankings_unmatched.txt — fix with
           add_alias.py and the next run picks it up.
  sherdog  Sherdog has no rankings page (/rankings 404s); its rankings are a
           multi-page NEWS ARTICLE — one division per page, picked from the
           article's page <select> — plus two single-page P4P articles, all
           linked from the "sub-menu rankings" block on /news/rankings/list.
           Every entry links /fighter/<Name>-<id>, so the id is exact; it's
           only nulled when that fighter isn't in the DB (e.g. a Rizin
           atomweight from an org we don't scrape).

rank 0 = champion (UFC divisions only; Sherdog and the P4P lists have none).
previous_rank comes from the site's own movement marker ("Rank increased by
3" / "[6]"); is_new marks an NR / new entry.

Fight tags. After the scrape, every UFC fight that hasn't happened yet gets
fights.fighter1_rank / fighter2_rank set from the latest UFC snapshot ('C' for
the champion, '1'..'15', null when unranked) — the rank each fighter carries
INTO the bout, the way the broadcast shows it. Fights dated before today are
never touched, so the day after an event its tags are frozen at fight-night
values. Nothing is backfilled for past fights (historical ranks are a separate
job — Sherdog keeps a monthly archive). A fighter ranked in a different
division from the bout (moving up/down) shows that standing.

  python rankings_scraper.py                 # both sources; new snapshot if changed
  python rankings_scraper.py --source ufc    # one source
  python rankings_scraper.py --force         # store today's snapshot even if unchanged
  python rankings_scraper.py --dry-run       # print the lists + planned tags, write nothing
  python rankings_scraper.py --tag-only      # skip scraping; re-tag upcoming UFC fights
"""
import re
import sys
import json
import time
import argparse
import unicodedata
import datetime as dt
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

import sherdog_scraper as s   # reuse _headers, USER_AGENT, SHERDOG_BASE, id_from_url, env load

UFC_BASE = 'https://www.ufc.com'
UFC_RANKINGS_URL = f'{UFC_BASE}/rankings'
SHERDOG_LIST_URL = f'{s.SHERDOG_BASE}/news/rankings/list'
UNMATCHED_FILE = 'rankings_unmatched.txt'
SLEEP = 1.0                     # seconds between Sherdog article pages

# Snapshot dates are Pacific calendar days: the nightly runs at 11:30 pm PST, so
# a UTC date would label every Saturday-night change with Sunday/Monday.
LOCAL_TZ = ZoneInfo('America/Los_Angeles')

MENS_P4P = "Men's Pound-for-Pound"
WOMENS_P4P = "Women's Pound-for-Pound"

# Refuse to snapshot a source with fewer rows than this — UFC is ~200 (13 lists
# of 15 + 11 champions), Sherdog ~150 (15 lists of 10). A layout change that
# breaks the parser must not be recorded as a real change.
MIN_ROWS = {'ufc': 100, 'sherdog': 60}

# Letters NFKD won't decompose to an ASCII base — mapped by hand before folding.
_FOLD_MAP = str.maketrans({'ł': 'l', 'Ł': 'l', 'ø': 'o', 'Ø': 'o', 'đ': 'd', 'Đ': 'd',
                           'ß': 'ss', 'æ': 'ae', 'œ': 'oe', '’': "'", '‘': "'"})


def fold(name):
    """Lowercase, accent-stripped, punctuation-free key for name matching."""
    n = unicodedata.normalize('NFKD', (name or '').translate(_FOLD_MAP))
    n = ''.join(c for c in n if not unicodedata.combining(c)).lower()
    n = re.sub(r"[.'\"]", '', n).replace('-', ' ')
    return re.sub(r'\s+', ' ', n).strip()


def canonical_division(label):
    """'Women’s Flyweight' / "Men's Pound-for-Pound Top Rank" -> canonical name."""
    d = re.sub(r'\s+', ' ', (label or '').replace('’', "'")).strip()
    return re.sub(r'\s*Top Rank$', '', d, flags=re.I)


def fetch_html(url):
    r = requests.get(url, headers={'User-Agent': s.USER_AGENT}, timeout=30)
    r.raise_for_status()
    return r.text


def fetch_all(table, select, extra='', order='id'):
    """Page through a Supabase table."""
    PAGE, off, out = 1000, 0, []
    while True:
        r = requests.get(f'{s.SUPABASE_URL}/rest/v1/{table}?select={select}'
                         f'{extra}&order={order}&limit={PAGE}&offset={off}',
                         headers=s._headers())
        r.raise_for_status()
        batch = r.json()
        out += batch
        if len(batch) < PAGE:
            return out
        off += PAGE


def _abs(base, href):
    if not href:
        return None
    return href if href.startswith('http') else base + href


# ── UFC ───────────────────────────────────────────────────────────────────────

def parse_ufc(html):
    """Ranking rows off ufc.com/rankings (fighter_id still unresolved)."""
    soup = BeautifulSoup(html, 'html.parser')
    rows, seen = [], set()
    for g in soup.select('.view-grouping'):
        hdr = g.select_one('.view-grouping-header')
        if not hdr:
            continue
        division = canonical_division(hdr.get_text(' ', strip=True))
        if not division or division in seen:
            continue            # the page repeats every division for its mobile layout
        seen.add(division)
        p4p = 'pound' in division.lower()

        # Champion block. On the P4P lists the same block just shows the #1,
        # who is also row 1 of the table — no champion slot there.
        champ = g.select_one('.rankings--athlete--champion h5 a')
        if champ and not p4p:
            rows.append({'division': division, 'rank': 0,
                         'fighter_name': champ.get_text(' ', strip=True),
                         'previous_rank': None, 'is_new': False,
                         'source_url': _abs(UFC_BASE, champ.get('href'))})

        for tr in g.select('tbody tr'):
            tds = tr.find_all('td')
            if len(tds) < 2:
                continue
            try:
                rank = int(tds[0].get_text(strip=True))
            except ValueError:
                continue
            a = tds[1].find('a')
            name = re.sub(r'\s+', ' ', (a or tds[1]).get_text(' ', strip=True))
            change = tds[2].get_text(' ', strip=True) if len(tds) > 2 else ''
            prev, new = rank, False
            m = re.search(r'Rank (increased|decreased) by (\d+)', change, re.I)
            if m:
                n = int(m.group(2))
                prev = rank + n if m.group(1).lower() == 'increased' else rank - n
            elif change and (change.upper().startswith('NR') or 'new' in change.lower()):
                prev, new = None, True
            rows.append({'division': division, 'rank': rank, 'fighter_name': name,
                         'previous_rank': prev, 'is_new': new,
                         'source_url': _abs(UFC_BASE, a.get('href')) if a else None})
    return rows


def ufc_roster():
    """Ids of every fighter with a UFC bout — the disambiguation pool."""
    orgs = requests.get(f'{s.SUPABASE_URL}/rest/v1/organizations?select=id&name=eq.UFC',
                        headers=s._headers()).json()
    if not orgs:
        return set()
    events = [e['id'] for e in fetch_all('events', 'id', f'&organization_id=eq.{orgs[0]["id"]}')]
    ids = set()
    for i in range(0, len(events), 150):
        chunk = ','.join(str(e) for e in events[i:i + 150])
        for f in fetch_all('fights', 'id,fighter1_id,fighter2_id', f'&event_id=in.({chunk})'):
            ids.update((f['fighter1_id'], f['fighter2_id']))
    ids.discard(None)
    return ids


class Resolver:
    """ufc.com display name -> fighters.id."""

    def __init__(self):
        fighters = fetch_all('fighters', 'id,name')
        aliases = fetch_all('fighter_aliases', 'alias,fighter_id', order='alias')
        self.names = {f['id']: f['name'] for f in fighters}
        self.by_key = {}
        for f in fighters:
            self.by_key.setdefault(fold(f['name']), []).append(f['id'])
        self.by_alias = {fold(a['alias']): a['fighter_id'] for a in aliases}
        self.roster = ufc_roster()
        self.tokens = {fid: set(fold(self.names[fid]).split())
                       for fid in self.roster if fid in self.names}

    def resolve(self, name):
        key = fold(name)
        toks = key.split()
        ids = list(self.by_key.get(key, []))
        if not ids and key in self.by_alias:
            ids = [self.by_alias[key]]
        if not ids and len(toks) == 2:           # "Song Yadong" <-> "Yadong Song"
            ids = list(self.by_key.get(' '.join(reversed(toks)), []))
        if len(ids) > 1:                          # same-name fighters: keep the UFC one
            in_ufc = [i for i in ids if i in self.roster]
            if len(in_ufc) == 1:
                ids = in_ufc
        if len(ids) == 1:
            return ids[0]
        if ids:
            return None                           # still ambiguous — leave for a human
        # Partial names against the UFC roster only: two shared tokens
        # ("Ian Machado Garry" / "Ian Garry"), else one name containing the
        # other ("Mizuki" / "Mizuki Inoue"). Must be unique to count.
        ts = set(toks)
        cands = [fid for fid, t in self.tokens.items() if len(ts & t) >= 2]
        if not cands:
            cands = [fid for fid, t in self.tokens.items() if ts <= t or t <= ts]
        return cands[0] if len(cands) == 1 else None


def scrape_ufc():
    print('UFC: fetching rankings page...')
    rows = parse_ufc(fetch_html(UFC_RANKINGS_URL))
    print(f'  {len(rows)} entries across {len({r["division"] for r in rows})} lists; resolving names...')
    res = Resolver()
    unmatched = []
    for r in rows:
        r['fighter_id'] = res.resolve(r['fighter_name'])
        if r['fighter_id'] is None:
            unmatched.append(r)
    for r in unmatched:
        print(f'  [UNMATCHED] {r["division"]:<24} #{r["rank"]:<3} {r["fighter_name"]}')
    print(f'  {len(rows) - len(unmatched)}/{len(rows)} matched to a fighter')
    return rows, unmatched


# ── Sherdog ───────────────────────────────────────────────────────────────────

RANK_H2 = re.compile(r'^\s*(\d+)\.')
NICKNAME = re.compile(r'\s*[“"][^”"]*[”"]\s*')


def parse_sherdog_page(html):
    """Ranked entries on one Sherdog rankings article page.

    Each entry is an <h2> shaped like
      1. <a href="/fighter/Ciryl-Gane-293973">Ciryl Gane</a> (14-2, 1 NC) | UFC [1]
    where [n] is last month's rank.
    """
    soup = BeautifulSoup(html, 'html.parser')
    out = []
    for h2 in soup.find_all('h2'):
        text = re.sub(r'\s+', ' ', h2.get_text(' ', strip=True))
        m = RANK_H2.match(text)
        if not m:
            continue
        a = h2.find('a', href=re.compile(r'/fighter/'))
        if not a:
            continue
        name = NICKNAME.sub(' ', re.sub(r'\s+', ' ', a.get_text(' ', strip=True))).strip()
        rec = re.search(r'\(([^)]*)\)', text)
        promo = re.search(r'\|\s*([^\[\]]+?)\s*(?:\[|$)', text)
        prev = re.search(r'\[([^\]]*)\]\s*$', text)
        prev_txt = prev.group(1).strip() if prev else ''
        out.append({'rank': int(m.group(1)), 'fighter_name': name,
                    'sherdog_id': s.id_from_url(a['href']),
                    'record': rec.group(1).strip() if rec else None,
                    'promotion': promo.group(1).strip() if promo else None,
                    'previous_rank': int(prev_txt) if prev_txt.isdigit() else None,
                    'is_new': bool(prev_txt) and not prev_txt.isdigit(),
                    'source_url': _abs(s.SHERDOG_BASE, a['href'])})
    return out


def sherdog_entry_points():
    """Current divisional / men's P4P / women's P4P article URLs."""
    soup = BeautifulSoup(fetch_html(SHERDOG_LIST_URL), 'html.parser')
    links = {}
    for a in soup.select('.sub-menu.rankings a[href]'):
        t = a.get_text(' ', strip=True).upper()
        href = _abs(s.SHERDOG_BASE, a['href'])
        if 'DIVISIONAL' in t:
            links['divisional'] = href
        elif 'WOMEN' in t:
            links[WOMENS_P4P] = href
        elif 'POUND' in t:
            links[MENS_P4P] = href
    return links


def existing_fighter_ids(ids):
    ids = [i for i in set(ids) if i]
    found = set()
    for i in range(0, len(ids), 200):
        chunk = ','.join(str(x) for x in ids[i:i + 200])
        r = requests.get(f'{s.SUPABASE_URL}/rest/v1/fighters?select=id&id=in.({chunk})',
                         headers=s._headers())
        r.raise_for_status()
        found.update(x['id'] for x in r.json())
    return found


def scrape_sherdog():
    print('Sherdog: locating current rankings articles...')
    links = sherdog_entry_points()
    if 'divisional' not in links:
        raise RuntimeError('Sherdog rankings sub-menu not found on /news/rankings/list')
    rows = []

    # Divisional article: page 1 is the write-up, then one division per page.
    # The page <select> carries the division names, so it drives the crawl.
    first_url = links['divisional']
    first = fetch_html(first_url)
    soup = BeautifulSoup(first, 'html.parser')
    pages, seen = [], set()
    for o in soup.select('select option'):
        href = o.get('value') or ''
        if '/news/rankings/' not in href or href in seen:
            continue
        seen.add(href)
        label = o.get_text(' ', strip=True)
        pages.append((_abs(s.SHERDOG_BASE, href), label.split(' - ', 1)[-1]))
    print(f'  divisional article: {len(pages)} pages')
    for url, label in pages:
        division = canonical_division(label)
        if 'weight' not in division.lower():
            continue                                    # the write-up page
        html = first if url == first_url else fetch_html(url)
        entries = parse_sherdog_page(html)
        for r in entries:
            r['division'] = division
        rows += entries
        print(f'    {division:<22} {len(entries)} entries')
        time.sleep(SLEEP)

    for division in (MENS_P4P, WOMENS_P4P):
        if division not in links:
            print(f'  [WARN] no {division} article linked')
            continue
        entries = parse_sherdog_page(fetch_html(links[division]))
        for r in entries:
            r['division'] = division
        rows += entries
        print(f'    {division:<22} {len(entries)} entries')
        time.sleep(SLEEP)

    known = existing_fighter_ids(r['sherdog_id'] for r in rows)
    unmatched = []
    for r in rows:
        sid = r.pop('sherdog_id')
        r['fighter_id'] = sid if sid in known else None
        if r['fighter_id'] is None:
            unmatched.append(r)
            print(f'  [NOT IN DB] {r["division"]:<24} #{r["rank"]:<3} {r["fighter_name"]} '
                  f'({r["promotion"]}, sherdog {sid})')
    print(f'  {len(rows) - len(unmatched)}/{len(rows)} entries link to a fighter in the DB')
    return rows, unmatched


# ── Snapshots ─────────────────────────────────────────────────────────────────

# What makes two snapshots "the same": everything the site publishes about an
# entry. A changed movement marker or record counts as a new publication.
SNAPSHOT_FIELDS = ('division', 'rank', 'fighter_name', 'fighter_id', 'previous_rank',
                   'is_new', 'record', 'promotion')


def _signature(rows):
    return {tuple(r.get(k) for k in SNAPSHOT_FIELDS) for r in rows}


def latest_snapshot(source):
    """(snapshot_date, rows) of the newest stored snapshot for a source, or (None, [])."""
    r = requests.get(f'{s.SUPABASE_URL}/rest/v1/rankings?source=eq.{source}'
                     f'&select=snapshot_date&order=snapshot_date.desc&limit=1',
                     headers=s._headers())
    r.raise_for_status()
    if not r.json():
        return None, []
    date = r.json()[0]['snapshot_date']
    rows = fetch_all('rankings', ','.join(SNAPSHOT_FIELDS),
                     f'&source=eq.{source}&snapshot_date=eq.{date}')
    return date, rows


def write_snapshot(source, rows, force=False):
    """Store the lists as today's snapshot if they differ from the latest one.

    Older snapshots are never touched. A same-day re-run replaces that day's
    snapshot rather than colliding with it. Returns True when rows were written.
    """
    prev_date, prev_rows = latest_snapshot(source)
    if not force and prev_rows and _signature(prev_rows) == _signature(rows):
        print(f'  {source}: unchanged since the {prev_date} snapshot — nothing written')
        return False

    now = dt.datetime.now(dt.timezone.utc)
    today = now.astimezone(LOCAL_TZ).date().isoformat()
    payload = [{'source': source, 'snapshot_date': today, 'division': r['division'],
                'rank': r['rank'], 'fighter_id': r.get('fighter_id'),
                'fighter_name': r['fighter_name'], 'previous_rank': r.get('previous_rank'),
                'is_new': bool(r.get('is_new')), 'record': r.get('record'),
                'promotion': r.get('promotion'), 'source_url': r.get('source_url'),
                'scraped_at': now.isoformat()} for r in rows]
    r = requests.delete(f'{s.SUPABASE_URL}/rest/v1/rankings?source=eq.{source}'
                        f'&snapshot_date=eq.{today}', headers=s._headers())
    if r.status_code not in (200, 204):
        raise RuntimeError(f'delete rankings/{source}/{today} failed ({r.status_code}): {r.text[:300]}')
    r = requests.post(f'{s.SUPABASE_URL}/rest/v1/rankings', headers=s._headers(),
                      data=json.dumps(payload))
    if r.status_code not in (200, 201):
        raise RuntimeError(f'insert rankings/{source} failed ({r.status_code}): {r.text[:300]}')
    since = f' (previous snapshot: {prev_date})' if prev_date and prev_date != today else ''
    print(f'  {source}: stored {len(payload)} rows as the {today} snapshot{since}')
    return True


# ── Fight tags ────────────────────────────────────────────────────────────────

def _ufc_org_id():
    orgs = requests.get(f'{s.SUPABASE_URL}/rest/v1/organizations?select=id&name=eq.UFC',
                        headers=s._headers()).json()
    return orgs[0]['id'] if orgs else None


def tag_upcoming_fights(dry_run=False):
    """Stamp upcoming UFC fights with each fighter's current UFC rank.

    Scope is fights on UFC events dated today or later — the lock-in is simply
    that a fight in the past is never selected again. Reads the latest stored
    UFC snapshot (not the page), so it works even when the scrape was skipped.
    """
    today = dt.datetime.now(LOCAL_TZ).date().isoformat()
    oid = _ufc_org_id()
    if oid is None:
        print('  [WARN] no UFC organization row — skipping fight tags')
        return
    events = fetch_all('events', 'id,name,date', f'&organization_id=eq.{oid}&date=gte.{today}')
    print(f'\nFight tags: {len(events)} upcoming UFC events')
    if not events:
        return
    ev_by_id = {e['id']: e for e in events}
    fights = []
    ids = list(ev_by_id)
    for i in range(0, len(ids), 150):
        chunk = ','.join(str(x) for x in ids[i:i + 150])
        fights += fetch_all('fights', 'id,event_id,weight_class,fighter1_id,fighter2_id,'
                                      'fighter1_rank,fighter2_rank', f'&event_id=in.({chunk})')

    # fighter -> {division: rank} off the latest UFC snapshot; P4P lists excluded.
    ranked = fetch_all('rankings_current', 'id,fighter_id,division,rank',
                       '&source=eq.ufc&fighter_id=not.is.null&division=not.ilike.*pound*')
    by_fighter = {}
    for r in ranked:
        by_fighter.setdefault(r['fighter_id'], {})[r['division']] = r['rank']

    def rank_for(fid, weight_class):
        divs = by_fighter.get(fid)
        if not divs:
            return None
        rank = divs.get(weight_class)
        if rank is None:                 # ranked, but in another division
            rank = min(divs.values())
        return 'C' if rank == 0 else str(rank)

    changes = []
    for f in fights:
        new1 = rank_for(f['fighter1_id'], f['weight_class'])
        new2 = rank_for(f['fighter2_id'], f['weight_class'])
        if new1 != f['fighter1_rank'] or new2 != f['fighter2_rank']:
            changes.append((f, new1, new2))
    tagged = sum(1 for f in fights if f['fighter1_rank'] or f['fighter2_rank'])
    print(f'  {len(fights)} fights, {tagged} already tagged, {len(changes)} to update')
    if not changes:
        return

    fids = {x for f, _, _ in changes for x in (f['fighter1_id'], f['fighter2_id']) if x}
    names = {}
    fl = list(fids)
    for i in range(0, len(fl), 200):
        chunk = ','.join(str(x) for x in fl[i:i + 200])
        names.update((x['id'], x['name']) for x in
                     fetch_all('fighters', 'id,name', f'&id=in.({chunk})'))
    fmt = lambda r: r or '-'
    for f, new1, new2 in sorted(changes, key=lambda c: ev_by_id[c[0]['event_id']]['date']):
        ev = ev_by_id[f['event_id']]
        print(f'  {ev["date"]} {ev["name"][:34]:<34} '
              f'{names.get(f["fighter1_id"], "?")} [{fmt(f["fighter1_rank"])}->{fmt(new1)}] vs '
              f'{names.get(f["fighter2_id"], "?")} [{fmt(f["fighter2_rank"])}->{fmt(new2)}]')
    if dry_run:
        return
    for f, new1, new2 in changes:
        r = requests.patch(f'{s.SUPABASE_URL}/rest/v1/fights?id=eq.{f["id"]}',
                           headers=s._headers(),
                           data=json.dumps({'fighter1_rank': new1, 'fighter2_rank': new2}))
        if r.status_code not in (200, 204):
            raise RuntimeError(f'tag fight {f["id"]} failed ({r.status_code}): {r.text[:300]}')
    print(f'  tagged {len(changes)} fights')


def print_rows(source, rows):
    by_div = {}
    for r in rows:
        by_div.setdefault(r['division'], []).append(r)
    for division, entries in by_div.items():
        print(f'\n  [{source}] {division}')
        for r in sorted(entries, key=lambda x: x['rank']):
            tag = ' C ' if r['rank'] == 0 else f'{r["rank"]:>2} '
            mv = 'NEW' if r.get('is_new') else (
                '' if r.get('previous_rank') in (None, r['rank'])
                else f'{r["previous_rank"] - r["rank"]:+d}')
            extra = ' · '.join(x for x in (r.get('record'), r.get('promotion')) if x)
            fid = r.get('fighter_id') or '—'
            print(f'    {tag} {r["fighter_name"]:<28} {mv:<4} {fid!s:<7} {extra}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--source', choices=('ufc', 'sherdog', 'all'), default='all')
    ap.add_argument('--force', action='store_true',
                    help="store today's snapshot even if nothing changed")
    ap.add_argument('--tag-only', action='store_true',
                    help='skip scraping; just re-tag upcoming UFC fights from the stored snapshot')
    ap.add_argument('--dry-run', action='store_true',
                    help='print the parsed lists and planned fight tags, write nothing')
    args = ap.parse_args()
    if not s.SUPABASE_URL or not s.SUPABASE_KEY:
        sys.exit('Missing SUPABASE creds in .env')

    scrapers = {'ufc': scrape_ufc, 'sherdog': scrape_sherdog}
    todo = [] if args.tag_only else (list(scrapers) if args.source == 'all' else [args.source])
    all_unmatched, failed = [], []
    for source in todo:
        try:
            rows, unmatched = scrapers[source]()
        except Exception as e:
            print(f'  [ERROR] {source}: {e}')
            failed.append(source)
            continue
        all_unmatched += [(source, r) for r in unmatched]
        if args.dry_run:
            print_rows(source, rows)
            continue
        if len(rows) < MIN_ROWS[source]:
            print(f'  [ERROR] {source}: only {len(rows)} rows parsed (< {MIN_ROWS[source]}) '
                  f'— not recording a snapshot')
            failed.append(source)
            continue
        write_snapshot(source, rows, force=args.force)

    # Tags come from the stored snapshot, so this runs even when the UFC lists
    # were unchanged (new fights get booked between ranking updates) or the
    # scrape failed. Sherdog-only runs skip it.
    if args.tag_only or 'ufc' in todo:
        try:
            tag_upcoming_fights(dry_run=args.dry_run)
        except Exception as e:
            print(f'  [ERROR] fight tags: {e}')
            failed.append('fight tags')

    if not args.dry_run and todo:
        with open(UNMATCHED_FILE, 'w', encoding='utf-8') as f:
            f.write('# Ranking entries with no fighters.id — add an alias (add_alias.py) '
                    'or scrape the org, then re-run.\n')
            for source, r in all_unmatched:
                f.write(f'{source}\t{r["division"]}\t#{r["rank"]}\t{r["fighter_name"]}'
                        f'\t{r.get("source_url") or ""}\n')
        print(f'\n{len(all_unmatched)} unmatched entries listed in {UNMATCHED_FILE}')
    if failed:
        sys.exit(f'Failed sources: {", ".join(failed)}')


if __name__ == '__main__':
    main()
