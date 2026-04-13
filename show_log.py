#!/usr/bin/env python3
"""
show_log.py
Print the most recent entries from the change_log table, with human-readable
context (fighter names, event names) looked up from the referenced tables.

Usage:
  python show_log.py            # last 20 entries
  python show_log.py 50         # last 50 entries
"""

import os
import sys
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
HEADERS = {'apikey': SUPABASE_KEY, 'Authorization': f'Bearer {SUPABASE_KEY}'}

limit = int(sys.argv[1]) if len(sys.argv) > 1 else 20

# ── 1. Load log entries ──────────────────────────────────────────────────────
resp = requests.get(
    f'{SUPABASE_URL}/rest/v1/change_log'
    f'?select=logged_at,table_name,action,row_id,changes'
    f'&order=logged_at.desc&limit={limit}',
    headers=HEADERS,
)
resp.raise_for_status()
rows = resp.json()

# ── 2. Look up referenced rows ───────────────────────────────────────────────
fight_ids = {r['row_id'] for r in rows if r['table_name'] in ('fights', 'fight_results') and r['row_id']}
event_ids = {r['row_id'] for r in rows if r['table_name'] == 'events' and r['row_id']}

fight_labels = {}  # fight_id -> "Fighter1 vs Fighter2 @ Event"
event_labels = {}  # event_id -> "Event Name (date)"

if fight_ids:
    ids_filter = ','.join(fight_ids)
    r = requests.get(
        f'{SUPABASE_URL}/rest/v1/fight_search'
        f'?id=in.({ids_filter})'
        f'&select=id,fighter1_name,fighter2_name,event_name,event_date',
        headers=HEADERS,
    )
    if r.ok:
        for f in r.json():
            fight_labels[f['id']] = (
                f'{f["fighter1_name"]} vs {f["fighter2_name"]} @ {f["event_name"]}'
            )

if event_ids:
    ids_filter = ','.join(event_ids)
    r = requests.get(
        f'{SUPABASE_URL}/rest/v1/events?id=in.({ids_filter})&select=id,name,date',
        headers=HEADERS,
    )
    if r.ok:
        for e in r.json():
            event_labels[e['id']] = f'{e["name"]} ({e["date"]})'

# ── 3. Print ─────────────────────────────────────────────────────────────────
print(f'Last {len(rows)} change_log entries:\n')

for r in rows:
    ts      = r['logged_at'][:19].replace('T', ' ')
    action  = r['action']
    table   = r['table_name']
    row_id  = r['row_id'] or ''
    changes = r['changes'] or {}

    if table in ('fights', 'fight_results'):
        label = fight_labels.get(row_id, '(unknown fight)')
    elif table == 'events':
        label = event_labels.get(row_id, '(unknown event)')
    else:
        label = ''

    print(f'[{ts}] {action:6} {table:14} {label}')
    print(f'    id: {row_id}')

    if action == 'UPDATE':
        for col, diff in changes.items():
            old = diff.get('old')
            new = diff.get('new')
            print(f'    {col}: {old!r} -> {new!r}')
    elif action in ('INSERT', 'DELETE'):
        summary = ', '.join(f'{k}={v!r}' for k, v in list(changes.items())[:5])
        print(f'    {summary}')
    print()
