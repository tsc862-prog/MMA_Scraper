#!/usr/bin/env python3
"""
add_alias.py
Add a fighter alias by canonical name.

Usage:
  python add_alias.py "Mirko Filipovic" "Mirko Cro Cop"
  python add_alias.py "Jose Aldo" "José Aldo"
"""

import os
import sys
import json
import requests

_env_path = os.path.join(os.path.dirname(__file__), '.env')
if os.path.exists(_env_path):
    with open(_env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip())

SUPABASE_URL = os.environ.get('SUPABASE_URL', '').rstrip('/')
SUPABASE_KEY = os.environ.get('SUPABASE_KEY', '')

def _headers():
    return {
        'apikey': SUPABASE_KEY,
        'Authorization': f'Bearer {SUPABASE_KEY}',
        'Content-Type': 'application/json',
    }

def find_fighter(name):
    resp = requests.get(
        f'{SUPABASE_URL}/rest/v1/fighters?name=ilike.{requests.utils.quote(name)}&select=id,name&limit=5',
        headers=_headers(),
    )
    return resp.json() if resp.ok else []

def add_alias(fighter_id, alias):
    resp = requests.post(
        f'{SUPABASE_URL}/rest/v1/fighter_aliases',
        headers={**_headers(), 'Prefer': 'resolution=merge-duplicates,return=minimal'},
        data=json.dumps({'alias': alias.lower(), 'fighter_id': fighter_id}),
    )
    return resp.status_code in (200, 201)

if len(sys.argv) != 3:
    print('Usage: python add_alias.py "Canonical Name" "Alias Name"')
    sys.exit(1)

canonical, alias = sys.argv[1], sys.argv[2]

matches = find_fighter(canonical)
if not matches:
    print(f'No fighter found matching "{canonical}"')
    sys.exit(1)

if len(matches) > 1:
    print(f'Multiple matches for "{canonical}" — pick one:')
    for i, f in enumerate(matches):
        print(f'  [{i}] {f["name"]} ({f["id"]})')
    choice = int(input('Enter number: '))
    fighter = matches[choice]
else:
    fighter = matches[0]

print(f'Mapping  "{alias}"  →  {fighter["name"]} ({fighter["id"]})')
if add_alias(fighter['id'], alias):
    print('Done.')
else:
    print('Failed — alias may already exist or fighter_id is invalid.')
