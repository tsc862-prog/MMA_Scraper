#!/usr/bin/env python3
"""
Sherdog Fight Scraper -> Supabase
Scrapes non-UFC promotions from sherdog.com and pushes results into Supabase.
Currently covers PFL and MVP. Mirrors start.py (the UFC/ufcstats scraper).

Setup:
  pip install requests beautifulsoup4
  Fill .env with SUPABASE_URL and SUPABASE_KEY
  Run: python sherdog_scraper.py
"""

import os
import re
import uuid
import time
import json
import requests
from datetime import datetime
from bs4 import BeautifulSoup

# Load .env
_env_path = os.path.join(os.path.dirname(__file__), '.env')
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith('#') and '=' in _line:
                _k, _v = _line.split('=', 1)
                os.environ.setdefault(_k.strip(), _v.strip())

# ── Config ────────────────────────────────────────────────────────────────────

SUPABASE_URL = os.environ.get('SUPABASE_URL', '').rstrip('/')
SUPABASE_KEY = os.environ.get('SUPABASE_KEY', '')

COMPLETED_EVENTS_FILE = 'sherdog_completed_events.txt'
SHERDOG_BASE = 'https://www.sherdog.com'

USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36')

# Fixed namespace for deterministic UUIDs — must match start.py
_NS = uuid.UUID('6ba7b810-9dad-11d1-80b4-00c04fd430c8')

# Each org: Sherdog org page, the slug keywords that identify its own events
# (the org page also links 'related events' from other promotions), the
# name-prefix cleanup, and the fight-id prefix (do NOT change once data exists).
# 'active': scraped by the daily run. Defunct orgs are inactive — scraped once
# with --all, then skipped (no new events ever).
SHERDOG_ORGS = [
    {
        'organization': 'PFL',
        'org_url':      f'{SHERDOG_BASE}/organizations/Professional-Fighters-League-12241',
        'slug_keys':    ('professional-fighters-league', 'pfl-'),
        'name_clean':   (r'^Professional Fighters League\s*-\s*', ''),
        'fight_prefix': 'pflfight',
        'active':       True,
    },
    {
        'organization': 'MVP',
        'org_url':      f'{SHERDOG_BASE}/organizations/Most-Valuable-Promotions-19337',
        'slug_keys':    ('most-valuable-promotions',),
        'name_clean':   (r'^Most Valuable Promotions\s*-\s*', 'MVP: '),
        'fight_prefix': 'mvpfight',
        'active':       True,
    },
    {
        # Defunct — folded into PFL. One-time scrape.
        'organization': 'Bellator',
        'org_url':      f'{SHERDOG_BASE}/organizations/Bellator-MMA-1960',
        'slug_keys':    ('bellator',),
        'name_clean':   (r'^Bellator MMA\s*-\s*', ''),
        'fight_prefix': 'bellatorfight',
        'active':       False,
    },
    {
        # Defunct — rebranded to PFL. One-time scrape.
        'organization': 'WSOF',
        'org_url':      f'{SHERDOG_BASE}/organizations/World-Series-of-Fighting-5449',
        'slug_keys':    ('wsof',),
        'name_clean':   (r'^World Series of Fighting\s*-\s*', ''),
        'fight_prefix': 'wsoffight',
        'active':       False,
    },
]

# ── Supabase REST helpers ─────────────────────────────────────────────────────

def _headers():
    return {
        'apikey': SUPABASE_KEY,
        'Authorization': f'Bearer {SUPABASE_KEY}',
        'Content-Type': 'application/json',
        'Prefer': 'resolution=merge-duplicates,return=minimal',
    }

def upsert(table, records):
    """Upsert one or more records into a Supabase table."""
    if not records:
        return
    if isinstance(records, dict):
        records = [records]
    resp = requests.post(
        f'{SUPABASE_URL}/rest/v1/{table}',
        headers=_headers(),
        data=json.dumps(records),
    )
    if resp.status_code not in (200, 201):
        # Raise — a failed push must abort before events get marked completed.
        raise RuntimeError(
            f'upsert {table} failed ({resp.status_code}): {resp.text[:300]}')

def fetch_female_fighter_names():
    """Lowercased names of fighters known to be female (for women's labels)."""
    names = set()
    offset = 0
    while True:
        resp = requests.get(
            f'{SUPABASE_URL}/rest/v1/fighters'
            f'?gender=eq.F&select=name&limit=1000&offset={offset}',
            headers=_headers(),
        )
        if not resp.ok:
            break
        batch = resp.json()
        names.update(r['name'].lower() for r in batch)
        if len(batch) < 1000:
            break
        offset += 1000
    return names

def rpc(name):
    """Call a parameterless Supabase stored procedure."""
    resp = requests.post(
        f'{SUPABASE_URL}/rest/v1/rpc/{name}',
        headers=_headers(),
        data='{}',
    )
    if resp.status_code not in (200, 204):
        print(f'  [WARN] rpc {name} failed ({resp.status_code}): {resp.text[:200]}')
        return None
    try:
        return resp.json()
    except Exception:
        return None

# ── ID helpers (shared scheme with start.py) ──────────────────────────────────

def event_uuid(name, date):
    return str(uuid.uuid5(_NS, f"event:{name}:{date or ''}"))

def fighter_uuid(name):
    return str(uuid.uuid5(_NS, f"fighter:{name}"))

def sherdog_fight_id(prefix, event_id, fighter_a, fighter_b):
    """Deterministic fight id — Sherdog has no per-fight URL like ufcstats."""
    pair = ':'.join(sorted([fighter_a or '', fighter_b or '']))
    return str(uuid.uuid5(_NS, f"{prefix}:{event_id}:{pair}"))

def split_method(raw):
    """'KO (Punch)' -> ('KO', 'Punch');  'Decision (Unanimous)' -> ('Decision', 'Unanimous')."""
    if not raw:
        return None, None
    m = re.match(r'^(.+?)\s*\((.+)\)\s*$', raw)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return raw.strip(), None

# ── Incremental tracking ──────────────────────────────────────────────────────

def load_completed_events():
    if not os.path.exists(COMPLETED_EVENTS_FILE):
        return set()
    with open(COMPLETED_EVENTS_FILE) as f:
        return set(line.strip() for line in f if line.strip())

def mark_event_completed(url):
    with open(COMPLETED_EVENTS_FILE, 'a') as f:
        f.write(url + '\n')

# ── Scraping ──────────────────────────────────────────────────────────────────

def _fetch(url):
    """GET a Sherdog page with a browser UA, following redirects."""
    return requests.get(url, headers={'User-Agent': USER_AGENT}, allow_redirects=True)

def get_event_urls(org):
    """
    Fetch one org's event URLs from its Sherdog organization page, following
    the 'Older Events' pagination (/recent-events/N) to the end.
    The org page also links 'related events' from other promotions — keep
    only slugs matching this org's keywords.
    """
    urls = []
    seen = set()
    page_url = org['org_url']
    visited = set()

    while page_url and page_url not in visited:
        visited.add(page_url)
        resp = _fetch(page_url)
        soup = BeautifulSoup(resp.text, 'html.parser')

        for a in soup.select('a[href^="/events/"]'):
            href = a.get('href')
            if not href or href in seen:
                continue
            slug = href.split('/events/', 1)[-1].lower()
            if any(k in slug for k in org['slug_keys']):
                seen.add(href)
                urls.append(SHERDOG_BASE + href)

        # Follow the "Older Events" pagination link, if any.
        page_url = None
        pag = soup.select_one('.pagination')
        if pag:
            for a in pag.find_all('a', href=True):
                if 'older' in a.get_text(strip=True).lower():
                    page_url = SHERDOG_BASE + a['href']
                    break
        time.sleep(0.3)

    return urls


def _clean_name(node):
    """Pull a fighter name out of a node, joining first/last split by <br>."""
    if not node:
        return None
    name = node.get_text(separator=' ', strip=True)
    return re.sub(r'\s+', ' ', name).strip() or None


def _iso_to_text(iso):
    """'2026-04-11T00:00:00+00:00' -> 'April 11, 2026' (matches UFC date format)."""
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso[:10]).strftime('%B %d, %Y').replace(' 0', ' ')
    except ValueError:
        return None


def parse_event_page(event_url, org):
    """
    Scrape one Sherdog event page. Returns a list of fight dicts — one per fight.
    fight_position 1 = main event. Upcoming fights have None for result fields.
    """
    resp = _fetch(event_url)
    if resp.status_code != 200:
        print(f'  SKIP (HTTP {resp.status_code}): {event_url}')
        return []

    soup = BeautifulSoup(resp.text, 'html.parser')

    # ── Event metadata ────────────────────────────────────────────────────────
    name_el = soup.find(itemprop='name')
    event_name = name_el.get_text(strip=True) if name_el else 'Unknown'
    pattern, replacement = org['name_clean']
    event_name = re.sub(pattern, replacement, event_name).strip()

    date_el = soup.find('meta', itemprop='startDate')
    event_date = _iso_to_text(date_el.get('content') if date_el else None)

    loc_el = soup.find(itemprop='location')
    event_location = loc_el.get_text(strip=True) if loc_el else None

    base = {
        'organization':   org['organization'],
        'fight_prefix':   org['fight_prefix'],
        'event_name':     event_name,
        'event_date':     event_date,
        'event_location': event_location,
    }

    fights = []

    # ── Main event (in .fight_card) ───────────────────────────────────────────
    fc = soup.select_one('.fight_card')
    if fc:
        m_names = [_clean_name(d.select_one('[itemprop="name"]'))
                   for d in fc.select('.fighter')[:2]]
        wc_el = fc.select_one('.weight_class')
        weight_class = wc_el.get_text(strip=True) if wc_el else None

        results = fc.select('.final_result')
        winner = None
        if len(m_names) == 2 and results:
            if 'win' in results[0].get('class', []):
                winner = m_names[0]
            elif len(results) > 1 and 'win' in results[1].get('class', []):
                winner = m_names[1]

        method = finish_round = finish_time = None
        resume = soup.select_one('.fight_card_resume')
        if resume:
            cells = {}
            for td in resume.find_all('td'):
                em = td.find('em')
                if em:
                    label = em.get_text(strip=True)
                    value = td.get_text(strip=True).replace(label, '', 1).strip()
                    cells[label] = value
            method       = cells.get('Method') or None
            finish_round = cells.get('Round') or None
            finish_time  = cells.get('Time') or None

        if len(m_names) == 2:
            fights.append({
                **base,
                'fight_position': 1,
                'fighter_a':      m_names[0],
                'fighter_b':      m_names[1],
                'winner':         winner,
                'weight_class':   weight_class,
                'method':         method,
                'finish_round':   finish_round,
                'finish_time':    finish_time,
            })

    # ── Undercard table ───────────────────────────────────────────────────────
    table = soup.select_one('table.new_table')
    if table:
        rows = table.find_all('tr')
        position = len(fights)  # main event already counted
        for tr in rows[1:]:  # skip header
            cells = tr.find_all('td')
            # Completed events: 7 cols (incl. Method/R/Time).
            # Upcoming events: 5 cols (Match, Fighter, Weight, Fighter, blank).
            if len(cells) < 5:
                continue
            position += 1

            fa = _clean_name(cells[1].select_one('a[href^="/fighter/"]'))
            fb = _clean_name(cells[3].select_one('a[href^="/fighter/"]'))
            if not fa or not fb:
                continue

            wc_el = cells[2].select_one('.weight_class')
            weight_class = wc_el.get_text(strip=True) if wc_el else None

            res_a = cells[1].select_one('.final_result')
            res_b = cells[3].select_one('.final_result')
            winner = None
            if res_a and 'win' in res_a.get('class', []):
                winner = fa
            elif res_b and 'win' in res_b.get('class', []):
                winner = fb

            # Result columns only exist on completed-event tables.
            method = rnd = t = None
            if len(cells) >= 7:
                method_el = cells[4].find('b')
                method = method_el.get_text(strip=True) if method_el else None
                rnd  = cells[5].get_text(strip=True)
                t    = cells[6].get_text(strip=True)

            fights.append({
                **base,
                'fight_position': position,
                'fighter_a':      fa,
                'fighter_b':      fb,
                'winner':         winner,
                'weight_class':   weight_class,
                'method':         method or None,
                'finish_round':   rnd or None,
                'finish_time':    t or None,
            })

    return fights

# ── Supabase push ─────────────────────────────────────────────────────────────

def push_fights(all_fights, female_names):
    """Batch upsert events, fighters, fights, fight_results."""
    events_map   = {}
    fighters_map = {}
    fights_map   = {}   # keyed by fight id — dedupes within the batch
    results_map  = {}   # keyed by fight id

    for f in all_fights:
        ev_id = event_uuid(f['event_name'], f['event_date'])
        a_id  = fighter_uuid(f['fighter_a']) if f['fighter_a'] else None
        b_id  = fighter_uuid(f['fighter_b']) if f['fighter_b'] else None
        w_id  = fighter_uuid(f['winner']) if f['winner'] else None
        fid   = sherdog_fight_id(f['fight_prefix'], ev_id, f['fighter_a'], f['fighter_b'])

        # Sherdog doesn't gender-label weight classes — prefix "Women's " when
        # a fighter is known female. recalculate_womens_divisions() backfills
        # any debutant women the female set doesn't yet cover.
        weight_class = f['weight_class']
        if weight_class and not weight_class.lower().startswith("women"):
            if ((f['fighter_a'] or '').lower() in female_names or
                    (f['fighter_b'] or '').lower() in female_names):
                weight_class = f"Women's {weight_class}"

        events_map[ev_id] = {
            'id':           ev_id,
            'name':         f['event_name'],
            'date':         f['event_date'],
            'location':     f['event_location'],
            'organization': f['organization'],
        }

        for name, uid in [(f['fighter_a'], a_id), (f['fighter_b'], b_id)]:
            if name and uid:
                fighters_map[uid] = {'id': uid, 'name': name}

        # Keep first occurrence — a fight reached via two redirect-equivalent
        # event URLs would otherwise collide on id within the upsert batch.
        if fid not in fights_map:
            fights_map[fid] = {
                'id':                  fid,
                'event_id':            ev_id,
                'fighter1_id':         a_id,
                'fighter2_id':         b_id,
                'weight_class':        weight_class,
                'is_main':             f['fight_position'] == 1,
                'fight_position':      f['fight_position'],
                'fight_position_type': 'Main Event' if f['fight_position'] == 1 else None,
            }

        # A finished fight has a method; upcoming ones don't.
        if f['method'] and fid not in results_map:
            method, details = split_method(f['method'])
            results_map[fid] = {
                'fight_id':  fid,
                'winner_id': w_id,
                'method':    method,
                'round':     int(f['finish_round']) if (f['finish_round'] or '').isdigit() else None,
                'time':      f['finish_time'],
                'details':   details,
            }

    upsert('events',        list(events_map.values()))
    upsert('fighters',      list(fighters_map.values()))
    upsert('fights',        list(fights_map.values()))
    upsert('fight_results', list(results_map.values()))

# ── Main ──────────────────────────────────────────────────────────────────────

def main(include_inactive=False):
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise ValueError('Missing SUPABASE_URL or SUPABASE_KEY in .env file')

    completed = load_completed_events()
    female_names = fetch_female_fighter_names()
    print(f'Loaded {len(female_names)} known female fighters')
    all_fights = []
    scraped_urls = []

    orgs = [o for o in SHERDOG_ORGS if o.get('active', True) or include_inactive]

    for org in orgs:
        print(f'\n=== {org["organization"]} ===')
        print('Fetching event list from Sherdog...')
        event_urls = get_event_urls(org)
        to_scrape = [u for u in event_urls if u not in completed]
        print(f'  {len(event_urls)} events found, '
              f'{len(event_urls) - len(to_scrape)} already scraped, '
              f'scraping {len(to_scrape)}...')

        for i, url in enumerate(to_scrape):
            print(f'  [{i+1}/{len(to_scrape)}] {url}')
            fights = parse_event_page(url, org)
            if fights:
                all_fights.extend(fights)
                # An event is "completed" once every fight has a result.
                if all(f['method'] for f in fights):
                    scraped_urls.append(url)
            time.sleep(0.5)

    if all_fights:
        print(f'\nPushing {len(all_fights)} fights to Supabase...')
        push_fights(all_fights, female_names)
        for url in scraped_urls:
            mark_event_completed(url)
        print('Removing orphan events...')
        rpc('delete_orphan_events')
        print('Recalculating debut flags...')
        rpc('recalculate_debut_flags')
        print('Recalculating women\'s divisions...')
        rpc('recalculate_womens_divisions')

    print(f'\nDone — pushed {len(all_fights)} fights to Supabase.')

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--all', action='store_true',
                        help='Include inactive (defunct) orgs like Bellator/WSOF')
    args = parser.parse_args()
    main(include_inactive=args.all)
