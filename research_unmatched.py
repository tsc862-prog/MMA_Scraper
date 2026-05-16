#!/usr/bin/env python3
"""
research_unmatched.py
Investigate the UNMATCHED section of paramount_matches.txt and suggest fixes.

For each fixable unmatched clip it reports:
  no_fighter(X) — searches the DB for fighters with a similar name (alias candidates)
  no_fight      — confirms both fighters exist, lists the fights each has on record

Usage:
  python research_unmatched.py
"""

import os
import re
import sys
import difflib
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
MATCHES_FILE = 'paramount_matches.txt'

sys.stdout.reconfigure(encoding='utf-8')

# ── Load DB ───────────────────────────────────────────────────────────────────

def fetch_all(table, select='*'):
    PAGE, offset, out = 1000, 0, []
    while True:
        r = requests.get(
            f'{SUPABASE_URL}/rest/v1/{table}?select={select}&limit={PAGE}&offset={offset}',
            headers=HEADERS,
        )
        if not r.ok:
            break
        batch = r.json()
        out.extend(batch)
        if len(batch) < PAGE:
            break
        offset += PAGE
    return out

print('Loading fighters / fights / events ...')
fighters = fetch_all('fighters', 'id,name')
aliases  = fetch_all('fighter_aliases', 'alias,fighter_id')
fights   = fetch_all('fights', 'id,event_id,fighter1_id,fighter2_id')
events   = fetch_all('events', 'id,name,date')

name_by_id    = {f['id']: f['name'] for f in fighters}
all_names     = [f['name'] for f in fighters]
name_lower    = {f['name'].lower(): f['id'] for f in fighters}
alias_lower   = {a['alias'].lower(): a['fighter_id'] for a in aliases}
event_by_id   = {e['id']: e for e in events}

# fighter_id -> list of (opponent_name, event_name)
fights_by_fighter = {}
for f in fights:
    f1, f2 = f['fighter1_id'], f['fighter2_id']
    ev = event_by_id.get(f['event_id'], {}).get('name', '?')
    if f1 and f2:
        fights_by_fighter.setdefault(f1, []).append((name_by_id.get(f2, '?'), ev))
        fights_by_fighter.setdefault(f2, []).append((name_by_id.get(f1, '?'), ev))

# ── Parse unmatched section ───────────────────────────────────────────────────

if not os.path.exists(MATCHES_FILE):
    print(f'{MATCHES_FILE} not found — run paramount_scraper.py first.')
    sys.exit(1)

with open(MATCHES_FILE, encoding='utf-8') as f:
    text = f.read()

if 'UNMATCHED' not in text:
    print('No UNMATCHED section found.')
    sys.exit(0)

unmatched_block = text.split('UNMATCHED', 1)[1]
lines = [ln.strip() for ln in unmatched_block.splitlines() if '|' in ln]

# Deduplicate
seen = set()
unique = []
for ln in lines:
    if ln not in seen:
        seen.add(ln)
        unique.append(ln)

print(f'{len(lines)} unmatched lines ({len(unique)} unique)\n')

# ── Helpers ───────────────────────────────────────────────────────────────────

def resolve(name):
    nl = name.lower().strip()
    if nl in name_lower:
        return name_lower[nl], 'exact'
    if nl in alias_lower:
        return alias_lower[nl], 'alias'
    return None, None

def suggest_names(name, n=5):
    """Closest DB fighter names to `name`."""
    return difflib.get_close_matches(name, all_names, n=n, cutoff=0.6)

# ── Report ────────────────────────────────────────────────────────────────────

no_fighter_cnt = no_fight_cnt = other_cnt = 0

for ln in unique:
    parts = [p.strip() for p in ln.split('|')]
    reason = parts[0]

    # ── no_fighter(X) ─────────────────────────────────────────────────────────
    if reason.startswith('no_fighter'):
        no_fighter_cnt += 1
        missing = re.search(r'no_fighter\((.+?)\)', reason)
        matchup = parts[1] if len(parts) > 1 else ''
        print(f'NO FIGHTER  | {matchup}')
        if missing:
            for nm in [m.strip() for m in missing.group(1).split(',')]:
                cands = suggest_names(nm)
                print(f'   "{nm}" not found. DB suggestions:')
                for c in cands:
                    print(f'      - {c}')
                if cands:
                    print(f'   → add_alias.py "{cands[0]}" "{nm}"')
        print()

    # ── no_fight ──────────────────────────────────────────────────────────────
    elif reason == 'no_fight':
        no_fight_cnt += 1
        matchup = parts[1] if len(parts) > 1 else ''
        print(f'NO FIGHT    | {matchup}')
        m = re.match(r'(.+?)\s+vs\.?\s+(.+)', matchup)
        if m:
            for nm in (m.group(1).strip(), m.group(2).strip()):
                fid, how = resolve(nm)
                if not fid:
                    print(f'   "{nm}" — not in DB')
                    for c in suggest_names(nm):
                        print(f'        maybe: {c}')
                else:
                    opps = fights_by_fighter.get(fid, [])
                    print(f'   "{nm}" ({how}) — {len(opps)} fights on record:')
                    for opp, ev in opps[:8]:
                        print(f'        vs {opp}  @ {ev}')
        print()

    else:
        other_cnt += 1

print('─' * 60)
print(f'no_fighter: {no_fighter_cnt}   no_fight: {no_fight_cnt}   other (skipped): {other_cnt}')
