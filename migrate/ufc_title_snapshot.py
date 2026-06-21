#!/usr/bin/env python3
"""
ufc_title_snapshot.py  (READ-ONLY)

Pre-migration safety net for UFC title flags.

Sherdog DOES mark title bouts on its event pages (span.title_fight), so the new
Sherdog-sourced scrape recovers is_title on its own. This snapshot is a FALLBACK:
it captures every UFC fight currently flagged is_title in the live DB (sourced
from ufcstats via start.py), keyed on the fighter pair + event, so the migration
can OR-merge any title flag Sherdog happens to miss on an old event.

Output: migrate/ufc_title_snapshot.json — a list of
  {"fighters": ["lower name a", "lower name b"], "event": "...", "date": "..."}

Usage (against PRODUCTION — only does SELECTs, never writes):
  python migrate/ufc_title_snapshot.py
"""

import os
import json
import requests

# ── Load .env (one dir up) ──────────────────────────────────────────────────
_env_path = os.path.join(os.path.dirname(__file__), '..', '.env')
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith('#') and '=' in _line:
                _k, _v = _line.split('=', 1)
                os.environ.setdefault(_k.strip(), _v.strip())

SUPABASE_URL = os.environ.get('SUPABASE_URL', '').rstrip('/')
SUPABASE_KEY = os.environ.get('SUPABASE_KEY', '')
OUT_PATH = os.path.join(os.path.dirname(__file__), 'ufc_title_snapshot.json')


def _headers():
    return {'apikey': SUPABASE_KEY, 'Authorization': f'Bearer {SUPABASE_KEY}'}


def fetch_all(table, select):
    """Read-only paginated GET (1000 rows/page)."""
    PAGE, offset, out = 1000, 0, []
    while True:
        url = f'{SUPABASE_URL}/rest/v1/{table}?select={select}&limit={PAGE}&offset={offset}'
        resp = requests.get(url, headers=_headers())
        resp.raise_for_status()
        batch = resp.json()
        out.extend(batch)
        if len(batch) < PAGE:
            return out
        offset += PAGE


def main():
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise SystemExit('Missing SUPABASE_URL / SUPABASE_KEY in .env')

    print('Loading fighters / events / title fights (read-only)...')
    fighters = {f['id']: f['name'] for f in fetch_all('fighters', 'id,name')}
    events = {e['id']: e for e in fetch_all('events', 'id,name,date')}
    # Only UFC events carried is_title historically (start.py). Pull all flagged.
    fights = fetch_all('fights', 'id,event_id,fighter1_id,fighter2_id,is_title')

    snapshot = []
    for fi in fights:
        if not fi.get('is_title'):
            continue
        a = fighters.get(fi['fighter1_id'])
        b = fighters.get(fi['fighter2_id'])
        ev = events.get(fi['event_id'], {})
        if not (a and b):
            continue
        snapshot.append({
            'fighters': sorted([a.lower(), b.lower()]),
            'event':    ev.get('name'),
            'date':     ev.get('date'),
        })

    with open(OUT_PATH, 'w', encoding='utf-8') as fh:
        json.dump(snapshot, fh, ensure_ascii=False, indent=2)

    print(f'Wrote {len(snapshot)} title-fight records -> {OUT_PATH}')


if __name__ == '__main__':
    main()
