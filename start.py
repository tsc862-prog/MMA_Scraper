#!/usr/bin/env python3
"""
UFC Fight Scraper -> Supabase
Incrementally scrapes ufcstats.com and pushes results into Supabase.

Setup:
  1. pip install requests beautifulsoup4  (already done)
  2. Fill in your .env file with SUPABASE_URL and SUPABASE_KEY
  3. Run: python start.py
"""

import os
import uuid
import time
import json
import requests
from bs4 import BeautifulSoup

# Load .env file manually — no extra packages needed
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

COMPLETED_EVENTS_FILE = 'completed_events.txt'
RECHECK_RECENT = 2  # always re-scrape the N most recent completed events

# Fixed namespace for deterministic UUIDs — do not change after first run
_NS = uuid.UUID('6ba7b810-9dad-11d1-80b4-00c04fd430c8')

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
        print(f'  [WARN] upsert {table} failed ({resp.status_code}): {resp.text[:200]}')

# ── UUID helpers ──────────────────────────────────────────────────────────────

def event_uuid(name, date):
    return str(uuid.uuid5(_NS, f"event:{name}:{date or ''}"))

def fighter_uuid(name):
    return str(uuid.uuid5(_NS, f"fighter:{name}"))

def fight_id(url):
    """Extract the hex ID from a ufcstats fight URL."""
    return url.rstrip('/').split('/')[-1]

# ── Method helpers ────────────────────────────────────────────────────────────

def broad_method(method):
    if not method:
        return None
    m = method.upper()
    if 'KO' in m or 'TKO' in m:
        return 'KO/TKO'
    if 'SUB' in m:
        return 'Submission'
    if 'DEC' in m:
        return 'Decision'
    if 'DQ' in m:
        return 'DQ'
    if 'NC' in m or 'NO CONTEST' in m:
        return 'No Contest'
    return method

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

def get_event_urls():
    """Fetch all completed + upcoming event URLs from ufcstats.com."""
    events = []
    for status, page in [('completed', 'completed'), ('upcoming', 'upcoming')]:
        url = f'http://www.ufcstats.com/statistics/events/{page}?page=all'
        resp = requests.get(url)
        soup = BeautifulSoup(resp.text, 'html.parser')
        for row in soup.find_all('tr', class_='b-statistics__table-row'):
            link = row.find('a', class_='b-link b-link_style_black')
            if link and link.get('href'):
                events.append((link['href'], status))
    return events


def parse_event_page(event_url, event_status):
    """
    Scrape one event page.
    Returns a list of fight dicts — one per fight on the card.
    fight_position 1 = main event. Upcoming fights have None for result fields.
    """
    resp = requests.get(event_url)
    if resp.status_code != 200:
        print(f'  SKIP (HTTP {resp.status_code}): {event_url}')
        return []

    soup = BeautifulSoup(resp.text, 'html.parser')

    title_el = soup.find('h2', class_='b-content__title')
    event_name = title_el.text.strip() if title_el else 'Unknown'

    event_date = event_location = None
    for item in soup.find_all('li', class_='b-list__box-list-item'):
        label = item.find('i', class_='b-list__box-item-title')
        if not label:
            continue
        value = item.get_text(strip=True).replace(label.get_text(strip=True), '').strip()
        if 'Date' in label.text:
            event_date = value
        elif 'Location' in label.text:
            event_location = value

    fights = []
    position = 0

    for row in soup.find_all('tr', class_='b-fight-details__table-row__hover'):
        fight_url = row.get('data-link')
        cells = row.find_all('td', class_='b-fight-details__table-col')
        if not cells or not fight_url:
            continue

        position += 1

        def cell_texts(idx):
            if idx >= len(cells):
                return []
            return [p.get_text(strip=True) for p in cells[idx].find_all('p')]

        wl     = cell_texts(0)
        names  = cell_texts(1)
        wclass = cell_texts(6)
        method = cell_texts(7)
        rnd    = cell_texts(8)
        t      = cell_texts(9)

        r_fighter = names[0] if names else None
        b_fighter = names[1] if len(names) > 1 else None

        winner = None
        if wl:
            if 'win' in wl[0].lower():
                winner = r_fighter
            elif len(wl) > 1 and 'win' in wl[1].lower():
                winner = b_fighter
            elif 'draw' in wl[0].lower() or 'nc' in wl[0].lower():
                winner = 'Draw/NC'

        wc_text = wclass[0] if wclass else ''
        weight_class = wc_text.replace(' Bout', '').replace(' Title', '').replace('UFC ', '').strip()

        fights.append({
            'event_name':     event_name,
            'event_date':     event_date,
            'event_location': event_location,
            'event_status':   event_status,
            'fight_position': position,
            'r_fighter':      r_fighter,
            'b_fighter':      b_fighter,
            'winner':         winner,
            'is_title_bout':  bool(cells[6].find('img', src=lambda s: s and 'belt' in s)),
            'weight_class':   weight_class,
            'method':         method[0] if method else None,
            'finish_round':   rnd[0] if rnd else None,
            'finish_time':    t[0] if t else None,
            'fight_url':      fight_url,
        })

    return fights

# ── Supabase push ─────────────────────────────────────────────────────────────

def rpc(name):
    """Call a parameterless Supabase stored procedure."""
    resp = requests.post(
        f'{SUPABASE_URL}/rest/v1/rpc/{name}',
        headers=_headers(),
        data='{}',
    )
    if resp.status_code not in (200, 204):
        print(f'  [WARN] rpc {name} failed ({resp.status_code}): {resp.text[:200]}')

def push_fights(fights):
    """Batch upsert all records — 4 HTTP requests total regardless of fight count."""
    events_map   = {}  # keyed by id to deduplicate
    fighters_map = {}
    fights_list  = []
    results_list = []

    for f in fights:
        ev_id = event_uuid(f['event_name'], f['event_date'])
        r_id  = fighter_uuid(f['r_fighter']) if f['r_fighter'] else None
        b_id  = fighter_uuid(f['b_fighter']) if f['b_fighter'] else None
        w_id  = fighter_uuid(f['winner']) if f['winner'] and f['winner'] != 'Draw/NC' else None
        fid   = fight_id(f['fight_url'])

        events_map[ev_id] = {
            'id':       ev_id,
            'name':     f['event_name'],
            'date':     f['event_date'],
            'location': f['event_location'],
        }

        for name, uid in [(f['r_fighter'], r_id), (f['b_fighter'], b_id)]:
            if name and uid:
                fighters_map[uid] = {'id': uid, 'name': name}

        fights_list.append({
            'id':                  fid,
            'event_id':            ev_id,
            'fighter1_id':         r_id,
            'fighter2_id':         b_id,
            'weight_class':        f['weight_class'],
            'is_main':             f['fight_position'] == 1,
            'is_title':            f['is_title_bout'],
            'fight_position':      f['fight_position'],
            'fight_position_type': 'Main Event' if f['fight_position'] == 1 else None,
        })

        if f['event_status'] == 'completed' and f['method']:
            results_list.append({
                'fight_id':     fid,
                'winner_id':    w_id,
                'method':       f['method'],
                'method_broad': broad_method(f['method']),
                'round':        int(f['finish_round']) if f['finish_round'] else None,
                'time':         f['finish_time'],
            })

    upsert('events',       list(events_map.values()))
    upsert('fighters',     list(fighters_map.values()))
    upsert('fights',       fights_list)
    upsert('fight_results', results_list)

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise ValueError('Missing SUPABASE_URL or SUPABASE_KEY in .env file')

    completed = load_completed_events()

    print('Fetching event list...')
    all_events = get_event_urls()

    # The most recent N completed events get re-scraped even if already done,
    # so that late result updates (decisions overturned, etc.) are captured.
    recent_completed = [
        url for url, status in all_events if status == 'completed'
    ][:RECHECK_RECENT]
    recheck = set(recent_completed)

    # Skip completed events already scraped; always re-scrape upcoming + recent
    to_scrape = [
        (url, status) for url, status in all_events
        if status == 'upcoming' or url not in completed or url in recheck
    ]

    print(f'Re-checking {len(recheck & completed)} recent completed events')
    print(f'Skipping {len(all_events) - len(to_scrape)} already-completed events')
    print(f'Scraping {len(to_scrape)} events...\n')

    all_fights = []
    completed_urls = []

    for i, (url, status) in enumerate(to_scrape):
        print(f'[{i+1}/{len(to_scrape)}] ({status}) {url}')
        fights = parse_event_page(url, status)
        if fights:
            all_fights.extend(fights)
            if status == 'completed':
                completed_urls.append(url)
        time.sleep(0.5)

    if all_fights:
        print(f'\nPushing {len(all_fights)} fights to Supabase...')
        push_fights(all_fights)
        for url in completed_urls:
            mark_event_completed(url)
        print('Recalculating debut flags...')
        rpc('recalculate_debut_flags')

    print(f'\nDone — pushed {len(all_fights)} fights to Supabase.')

if __name__ == '__main__':
    main()
