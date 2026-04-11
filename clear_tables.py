#!/usr/bin/env python3
"""
Delete all rows from the UFC Supabase tables.
Order matters: child tables first to avoid FK constraint errors.
"""

import os
import json
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

def _headers():
    return {
        'apikey': SUPABASE_KEY,
        'Authorization': f'Bearer {SUPABASE_KEY}',
        'Content-Type': 'application/json',
        'Prefer': 'return=minimal',
    }

def delete_all(table, pk='id'):
    resp = requests.delete(
        f'{SUPABASE_URL}/rest/v1/{table}?{pk}=not.is.null',
        headers=_headers(),
    )
    if resp.status_code in (200, 204):
        print(f'  Cleared: {table}')
    else:
        print(f'  [WARN] {table} ({resp.status_code}): {resp.text[:200]}')

if __name__ == '__main__':
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise ValueError('Missing SUPABASE_URL or SUPABASE_KEY in .env file')

    print('Clearing tables...')

    # Delete in dependency order (children before parents)
    delete_all('fight_results', pk='fight_id')  # PK is fight_id, references fights
    delete_all('ratings')                        # references fights
    delete_all('fights')                         # references events + fighters
    delete_all('events')
    delete_all('fighters')

    # Also reset the local completed-events tracker
    tracker = 'completed_events.txt'
    if os.path.exists(tracker):
        os.remove(tracker)
        print(f'  Deleted: {tracker}')

    print('Done.')
