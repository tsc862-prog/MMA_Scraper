#!/usr/bin/env python3
"""
backup_prod.py — logical backup of the production DB to local JSONL.

Safety net BEFORE the in-place cutover. Dumps every core table over the REST API
(read-only) so the pre-migration state is fully restorable. Run against PROD
(creds from ../.env).

  python migrate/backup_prod.py                 # -> migrate/backup/<stamp>/<table>.jsonl

Restore (if ever needed) is a straight re-insert of each file's rows.
"""
import os
import sys
import json
import requests

_env = os.path.join(os.path.dirname(__file__), '..', '.env')
E = {}
for line in open(_env):
    line = line.strip()
    if line and not line.startswith('#') and '=' in line:
        k, v = line.split('=', 1)
        E[k.strip()] = v.strip()
URL = E['SUPABASE_URL'].rstrip('/')
KEY = E['SUPABASE_KEY']
H = {'apikey': KEY, 'Authorization': f'Bearer {KEY}'}

# Core tables (excludes change_log audit, which is regenerable).
TABLES = ['fighters', 'fighter_aliases', 'events', 'fights',
          'fight_results', 'ratings', 'review_queue']


def dump(table, outdir):
    path = os.path.join(outdir, f'{table}.jsonl')
    n, off = 0, 0
    with open(path, 'w', encoding='utf-8') as fh:
        while True:
            r = requests.get(f'{URL}/rest/v1/{table}?select=*&limit=1000&offset={off}', headers=H)
            r.raise_for_status()
            batch = r.json()
            for row in batch:
                fh.write(json.dumps(row, ensure_ascii=False) + '\n')
            n += len(batch)
            if len(batch) < 1000:
                break
            off += 1000
    print(f'  {table}: {n} rows -> {path}')
    return n


def main():
    stamp = sys.argv[1] if len(sys.argv) > 1 else 'prod'   # pass a timestamp from caller
    outdir = os.path.join(os.path.dirname(__file__), 'backup', stamp)
    os.makedirs(outdir, exist_ok=True)
    print(f'Backing up PROD ({URL.split("//")[-1]}) -> {outdir}')
    total = sum(dump(t, outdir) for t in TABLES)
    print(f'Done — {total} rows backed up.')


if __name__ == '__main__':
    main()
