#!/usr/bin/env python3
"""
mvp_insert.py
One-off insert of MVP (Most Valuable Promotions) MMA events into Supabase.
MVP is boxing-first; this only covers their MMA cards.

Update the EVENTS list below as new MVP MMA cards are announced, then re-run.
Records are upserted, so re-running is safe.
"""

import os
import uuid
import json
import requests

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
ORGANIZATION = 'MVP'
_NS = uuid.UUID('6ba7b810-9dad-11d1-80b4-00c04fd430c8')

# ── MVP MMA cards ─────────────────────────────────────────────────────────────
# Each fight: (fighter_a, fighter_b, weight_class). Position 1 = main event.
EVENTS = [
    {
        'name':     'MVP MMA 1: Rousey vs. Carano',
        'date':     'May 16, 2026',
        'location': 'Intuit Dome, Inglewood, California',
        'fights': [
            ('Ronda Rousey',       'Gina Carano',         None),
            ('Nate Diaz',          'Mike Perry',          None),
            ('Francis Ngannou',    'Philipe Lins',        None),
            ('Salahdine Parnasse', 'Kenny Cross',         None),
            ('Junior dos Santos',  'Robelis Despaigne',   None),
            ('Namo Fazil',         'Jake Babian',         None),
            ('Adriano Moraes',     'Phumi Nkuta',         None),
            ('Jason Jackson',      'Jeff Creighton',      None),
            ('David Mgoyan',       'Albert Morales',      None),
            ('Aline Pereira',      'Jade Masson-Wong',    None),
            ('Chris Avila',        'Brandon Jenkins',     None),
            ('Lorenz Larkin',      'Muhammad Mokaev',     None),
        ],
    },
]

# ── Helpers (shared UUID scheme with start.py / pfl_scraper.py) ────────────────

def _headers():
    return {
        'apikey': SUPABASE_KEY,
        'Authorization': f'Bearer {SUPABASE_KEY}',
        'Content-Type': 'application/json',
        'Prefer': 'resolution=merge-duplicates,return=minimal',
    }

def upsert(table, records):
    if not records:
        return
    resp = requests.post(
        f'{SUPABASE_URL}/rest/v1/{table}',
        headers=_headers(),
        data=json.dumps(records),
    )
    if resp.status_code not in (200, 201):
        print(f'  [WARN] upsert {table} failed ({resp.status_code}): {resp.text[:200]}')

def event_uuid(name, date):
    return str(uuid.uuid5(_NS, f"event:{name}:{date or ''}"))

def fighter_uuid(name):
    return str(uuid.uuid5(_NS, f"fighter:{name}"))

def fight_uuid(event_id, a, b):
    pair = ':'.join(sorted([a or '', b or '']))
    return str(uuid.uuid5(_NS, f"mvpfight:{event_id}:{pair}"))

# ── Build + push ──────────────────────────────────────────────────────────────

def main():
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise ValueError('Missing SUPABASE_URL or SUPABASE_KEY in .env file')

    events_list  = []
    fighters_map = {}
    fights_list  = []

    for ev in EVENTS:
        ev_id = event_uuid(ev['name'], ev['date'])
        events_list.append({
            'id':           ev_id,
            'name':         ev['name'],
            'date':         ev['date'],
            'location':     ev['location'],
            'organization': ORGANIZATION,
        })

        for pos, (a, b, weight) in enumerate(ev['fights'], start=1):
            a_id = fighter_uuid(a)
            b_id = fighter_uuid(b)
            fighters_map[a_id] = {'id': a_id, 'name': a}
            fighters_map[b_id] = {'id': b_id, 'name': b}

            fights_list.append({
                'id':                  fight_uuid(ev_id, a, b),
                'event_id':            ev_id,
                'fighter1_id':         a_id,
                'fighter2_id':         b_id,
                'weight_class':        weight,
                'is_main':             pos == 1,
                'fight_position':      pos,
                'fight_position_type': 'Main Event' if pos == 1 else None,
            })

    print(f'Pushing {len(events_list)} MVP event(s), '
          f'{len(fighters_map)} fighters, {len(fights_list)} fights...')
    upsert('events',   events_list)
    upsert('fighters', list(fighters_map.values()))
    upsert('fights',   fights_list)
    print('Done.')

if __name__ == '__main__':
    main()
