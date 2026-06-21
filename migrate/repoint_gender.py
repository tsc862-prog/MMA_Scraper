#!/usr/bin/env python3
"""
repoint_gender.py — carry fighter gender across the Sherdog re-key.

fighters.gender / gender_checked is accumulated work (gender_scraper.py:
Wikidata + review-queue resolutions) and drives women's-division labeling.
Sherdog doesn't provide gender, so a rebuild starts blank. This re-applies the
OLD gender to the NEW Sherdog-keyed fighters by name.

Reads OLD gender from `fighters_old` (the snapshot 01_prepare.sql makes), matches
to NEW fighters by lowercased name. When several OLD fighters share a name, the
gender is applied only if they AGREE (conflicts are left blank for gender_scraper
to resolve, and reported).

Usage (after the rebuild, before/with the ratings repoint):
  python migrate/repoint_gender.py            # dry-run report
  python migrate/repoint_gender.py --apply
"""
import os
import sys
import json
import argparse
from collections import defaultdict

import requests
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import import_ratings as ir   # reuse SUPABASE_URL/KEY/_headers/fetch_all

OLD_FIGHTERS = 'fighters_old'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()
    if not ir.SUPABASE_URL or not ir.SUPABASE_KEY:
        sys.exit('Missing SUPABASE_URL / SUPABASE_KEY in .env')

    print('Loading OLD gender + NEW fighters...')
    old = ir.fetch_all(OLD_FIGHTERS, 'name,gender,gender_checked')
    new = ir.fetch_all('fighters', 'id,name,gender')

    # name -> set of (gender, gender_checked) from old
    by_name = defaultdict(set)
    for f in old:
        if f.get('gender'):
            by_name[f['name'].lower().strip()].add((f['gender'], bool(f.get('gender_checked'))))

    updates, conflicts = [], 0
    for f in new:
        vals = by_name.get((f.get('name') or '').lower().strip())
        if not vals:
            continue
        genders = {g for g, _ in vals}
        if len(genders) > 1:        # same name, disagreeing gender -> skip
            conflicts += 1
            continue
        gender = next(iter(genders))
        checked = any(c for _, c in vals)
        updates.append({'id': f['id'], 'gender': gender, 'gender_checked': checked})

    male = sum(1 for u in updates if u['gender'] == 'M')
    female = sum(1 for u in updates if u['gender'] == 'F')
    print(f'Will set gender on {len(updates)} fighters  (M={male} F={female}); '
          f'{conflicts} name-conflicts skipped (left for gender_scraper).')

    if not args.apply:
        print('Dry run — re-run with --apply to write.')
        return

    print('Applying...')
    for i, u in enumerate(updates):
        r = requests.patch(f'{ir.SUPABASE_URL}/rest/v1/fighters?id=eq.{u["id"]}',
                           headers=ir._headers(),
                           data=json.dumps({'gender': u['gender'],
                                            'gender_checked': u['gender_checked']}))
        if r.status_code not in (200, 204):
            sys.exit(f'  [FAIL] fighter {u["id"]} ({r.status_code}): {r.text[:200]}')
        if (i + 1) % 500 == 0:
            print(f'  {i+1}/{len(updates)}')
    print(f'Done — gender set on {len(updates)} fighters.')


if __name__ == '__main__':
    main()
