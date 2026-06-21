#!/usr/bin/env python3
"""
copy_old_snapshot.py  (one-off, staging build helper)

Copies from PROD into STAGING only the data the ratings repoint needs:
  - ratings            -> staging.ratings        (all 832; fight_id still OLD)
  - the fights they reference -> staging.fights_old
  - those fights' fighters    -> staging.fighters_old
  - those fights' events      -> staging.events_old

PROD creds come from ../.env (SUPABASE_URL / SUPABASE_KEY).
STAGING creds come from env vars STAGING_SUPABASE_URL / STAGING_SUPABASE_KEY.
"""
import os
import json
import requests

_env = os.path.join(os.path.dirname(__file__), '..', '.env')
PROD = {}
for line in open(_env):
    line = line.strip()
    if line and not line.startswith('#') and '=' in line:
        k, v = line.split('=', 1)
        PROD[k.strip()] = v.strip()

PROD_URL = PROD['SUPABASE_URL'].rstrip('/')
PROD_KEY = PROD['SUPABASE_KEY']
STG_URL = os.environ['STAGING_SUPABASE_URL'].rstrip('/')
STG_KEY = os.environ['STAGING_SUPABASE_KEY']


def h(key):
    return {'apikey': key, 'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'}


def prod_get(path):
    r = requests.get(f'{PROD_URL}/rest/v1/{path}', headers=h(PROD_KEY))
    r.raise_for_status()
    return r.json()


def prod_get_in(table, select, col, ids):
    """GET rows where col in ids, chunked to keep the URL short."""
    out = []
    ids = [i for i in ids if i is not None]
    for i in range(0, len(ids), 80):
        chunk = ids[i:i+80]
        vals = ','.join(f'"{c}"' for c in chunk)
        out += prod_get(f'{table}?select={select}&{col}=in.({vals})')
    return out


def stg_insert(table, rows):
    for i in range(0, len(rows), 500):
        batch = rows[i:i+500]
        r = requests.post(f'{STG_URL}/rest/v1/{table}',
                          headers={**h(STG_KEY), 'Prefer': 'resolution=merge-duplicates'},
                          data=json.dumps(batch))
        if r.status_code not in (200, 201, 204):
            raise SystemExit(f'insert {table} failed ({r.status_code}): {r.text[:300]}')
    print(f'  wrote {len(rows)} -> {table}')


def main():
    print('Reading prod ratings...')
    ratings = []
    offset = 0
    while True:
        batch = prod_get(f'ratings?select=id,fight_id,user_id,rating,notes,logged_at'
                         f'&limit=1000&offset={offset}')
        ratings += batch
        if len(batch) < 1000:
            break
        offset += 1000
    print(f'  {len(ratings)} ratings')
    fight_ids = sorted({r['fight_id'] for r in ratings if r['fight_id']})

    print('Reading referenced OLD fights...')
    fights = prod_get_in('fights', 'id,event_id,fighter1_id,fighter2_id', 'id', fight_ids)
    print(f'  {len(fights)} fights')

    fighter_ids = {f['fighter1_id'] for f in fights} | {f['fighter2_id'] for f in fights}
    event_ids = {f['event_id'] for f in fights}

    print('Reading referenced OLD fighters / events...')
    fighters = prod_get_in('fighters', 'id,name', 'id', list(fighter_ids))
    events = prod_get_in('events', 'id,name,date', 'id', list(event_ids))
    print(f'  {len(fighters)} fighters, {len(events)} events')

    print('Writing to staging...')
    stg_insert('fighters_old', fighters)
    stg_insert('events_old', events)
    stg_insert('fights_old', fights)
    stg_insert('ratings', ratings)
    print('Done.')


if __name__ == '__main__':
    main()
