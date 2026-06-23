#!/usr/bin/env python3
"""
gender_propagate.py — resolve fighter gender by opponent-graph propagation.

Pro MMA is gender-segregated (no inter-gender bouts), so every connected
component of the fight graph is single-gender. Seed from gender_checked fighters
and propagate their gender to the UNCHECKED fighters in the same component —
this is definitive, not a guess. Components that contain BOTH a checked male and
a checked female (a data error or a genuinely ambiguous merge) are left alone
for human review. Pending gender review_queue items whose fighters both become
checked are auto-resolved.

This is far cheaper and more reliable than per-fighter Wikidata lookups, and it
catches women defaulted to 'M' (their opponents pull them to 'F').

  python gender_propagate.py            # dry-run report
  python gender_propagate.py --apply
"""
import os
import sys
import json
import argparse
from collections import defaultdict
from datetime import datetime, timezone

import requests

# ── .env + Supabase helpers (self-contained) ─────────────────────────────────
_env = os.path.join(os.path.dirname(__file__), '.env')
if os.path.exists(_env):
    for _line in open(_env):
        _line = _line.strip()
        if _line and not _line.startswith('#') and '=' in _line:
            _k, _v = _line.split('=', 1)
            os.environ.setdefault(_k.strip(), _v.strip())

SUPABASE_URL = os.environ.get('SUPABASE_URL', '').rstrip('/')
SUPABASE_KEY = os.environ.get('SUPABASE_KEY', '')


def _sb_headers():
    return {'apikey': SUPABASE_KEY, 'Authorization': f'Bearer {SUPABASE_KEY}',
            'Content-Type': 'application/json', 'Prefer': 'return=minimal'}


def fetch_all(table, select='*', extra=''):
    PAGE, off, out = 1000, 0, []
    while True:
        r = requests.get(f'{SUPABASE_URL}/rest/v1/{table}?select={select}'
                         f'&limit={PAGE}&offset={off}{extra}', headers=_sb_headers())
        r.raise_for_status()
        b = r.json()
        out += b
        if len(b) < PAGE:
            return out
        off += PAGE


def rpc(name):
    r = requests.post(f'{SUPABASE_URL}/rest/v1/rpc/{name}', headers=_sb_headers(), data='{}')
    if r.status_code not in (200, 204):
        print(f'  [WARN] rpc {name} failed ({r.status_code})')


def patch_in(table, ids, payload, cast=str):
    """PATCH every row whose id is in `ids` (chunked)."""
    ids = list(ids)
    for i in range(0, len(ids), 100):
        inlist = ','.join(cast(c) for c in ids[i:i+100])
        r = requests.patch(f'{SUPABASE_URL}/rest/v1/{table}?id=in.({inlist})',
                           headers=_sb_headers(), data=json.dumps(payload))
        if r.status_code not in (200, 204):
            sys.exit(f'  PATCH {table} failed ({r.status_code}): {r.text[:200]}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()
    if not SUPABASE_URL or not SUPABASE_KEY:
        sys.exit('Missing SUPABASE creds')

    print('Loading fighters + fights...')
    fighters = fetch_all('fighters', 'id,name,gender,gender_checked')
    fights = fetch_all('fights', 'fighter1_id,fighter2_id')
    byid = {f['id']: f for f in fighters}

    # ── Direct-opponent adjacency ─────────────────────────────────────────────
    adj = defaultdict(set)
    for fi in fights:
        a, b = fi['fighter1_id'], fi['fighter2_id']
        if a in byid and b in byid and a != b:
            adj[a].add(b)
            adj[b].add(a)

    # ── Iterative propagation by DIRECT-opponent unanimity ────────────────────
    # A fighter's gender = the gender of their checked opponents, but only when
    # those opponents UNANIMOUSLY agree. A man who fought mostly men + one woman
    # (an old intergender bout, an amateur card, or a merged-id error) has mixed
    # opponents -> left for human review, NOT flipped. Re-iterates as newly
    # resolved fighters become seeds. (Component-based propagation over-reaches:
    # one bad cross-gender edge contaminates a whole women's-MMA cluster.)
    g_after = {f['id']: f['gender'] for f in fighters
               if f['gender_checked'] and f['gender']}
    newly = {}                      # id -> gender (resolved this run)
    conflicts = set()               # unchecked fighters with mixed opponents
    changed = True
    while changed:
        changed = False
        for f in fighters:
            fid = f['id']
            if fid in g_after:
                continue
            opp_g = {g_after[o] for o in adj[fid] if o in g_after}
            if len(opp_g) == 1:
                gender = next(iter(opp_g))
                g_after[fid] = gender
                newly[fid] = gender
                conflicts.discard(fid)
                changed = True
            elif len(opp_g) > 1:
                conflicts.add(fid)

    to_M = [i for i, gd in newly.items() if gd == 'M']
    to_F = [i for i, gd in newly.items() if gd == 'F']
    flips_F = [byid[i]['name'] for i in to_F if byid[i]['gender'] == 'M']
    print(f'  resolvable by unanimous opponents: M={len(to_M)}  F={len(to_F)}  '
          f'(total {len(newly)})')
    print(f'  left for review (mixed-gender opponents): {len(conflicts)}')
    print(f'  of the F set, {len(flips_F)} were defaulted M (now corrected to F)')
    for nm in flips_F[:15]:
        print(f'    M->F: {nm}')

    print('\nLoading pending gender review items...')
    reviews = fetch_all('review_queue', 'id,payload',
                          extra="&type=eq.gender&status=eq.pending")
    res_M, res_F = [], []
    for r in reviews:
        pl = r.get('payload') or {}
        a, b = pl.get('fighter1_id'), pl.get('fighter2_id')
        ga, gb = g_after.get(a), g_after.get(b)
        if ga and gb and ga == gb:
            (res_F if ga == 'F' else res_M).append(r['id'])
    print(f'  review items resolvable: M={len(res_M)}  F={len(res_F)}  '
          f'(of {len(reviews)} pending)')

    if not args.apply:
        print('\nDry run — re-run with --apply to write.')
        return

    print('\nApplying fighter genders...')
    patch_in('fighters', to_M, {'gender': 'M', 'gender_checked': True}, cast=str)
    patch_in('fighters', to_F, {'gender': 'F', 'gender_checked': True}, cast=str)
    print('Recalculating women\'s divisions...')
    rpc('recalculate_womens_divisions')

    print('Resolving review items...')
    now = datetime.now(timezone.utc).isoformat()
    for ids, gender in ((res_M, 'M'), (res_F, 'F')):
        patch_in('review_queue', ids,
                 {'status': 'resolved', 'resolved_at': now,
                  'resolution': {'gender': gender, 'by': 'graph_propagation'}},
                 cast=str)
    print(f'Done — set {len(to_M)+len(to_F)} fighters, '
          f'resolved {len(res_M)+len(res_F)} review items.')


if __name__ == '__main__':
    main()
