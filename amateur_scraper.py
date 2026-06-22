#!/usr/bin/env python3
"""
amateur_scraper.py — flag amateur fights from Sherdog FIGHTER pages.

Sherdog only marks a bout amateur on the fighter page (a "FIGHT HISTORY -
AMATEUR" section), never on the event page. So sherdog_scraper.py can only catch
whole amateur events by name ("... Amateur Series N"); amateur bouts on
otherwise-pro cards (e.g. Josiah Jones at "Fury FC 104") are invisible there.

This crawls fighter pages, reads each fighter's amateur section (each row links
to its event), and sets fights.is_amateur on the matching DB fight by
(fighter_id, event_id) — robust id-based matching, no fuzzy.

Resumable via amateur_checked.txt. Scope with --orgs to limit to fighters who
appear in given orgs (regional promotions run amateur cards; UFC/PFL/etc. don't).

  python amateur_scraper.py                 # all fighters
  python amateur_scraper.py --orgs "Fury FC,KOTC,LFA,CFFC,Shooto"
  python amateur_scraper.py --limit 500     # chunked
"""
import os
import re
import sys
import time
import json
import argparse
import requests
from bs4 import BeautifulSoup

import sherdog_scraper as s   # reuse _fetch, id_from_url, env load, SUPABASE_*

CHECKED_FILE = 'amateur_checked.txt'


def fetch_all(table, select):
    PAGE, off, out = 1000, 0, []
    while True:
        r = requests.get(f'{s.SUPABASE_URL}/rest/v1/{table}?select={select}'
                         f'&limit={PAGE}&offset={off}', headers=s._headers())
        r.raise_for_status()
        b = r.json()
        out += b
        if len(b) < PAGE:
            return out
        off += PAGE


def amateur_event_ids(fighter_id):
    """Event ids where this fighter has an AMATEUR bout (from the fighter page)."""
    resp = s._fetch(f'{s.SHERDOG_BASE}/fighter/x-{fighter_id}')
    if resp.status_code != 200:
        return None
    soup = BeautifulSoup(resp.text, 'html.parser')
    # Find the "FIGHT HISTORY - AMATEUR" section header, then the next fight table.
    hdr = soup.find(string=re.compile(r'fight history\s*-\s*amateur', re.I))
    if not hdr:
        return set()
    table = hdr.find_parent().find_next('table', class_='new_table')
    if not table:
        return set()
    ids = set()
    for tr in table.find_all('tr')[1:]:
        link = tr.select_one('a[href^="/events/"]')
        if link:
            eid = s.id_from_url(link.get('href'))
            if eid:
                ids.add(eid)
    return ids


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--orgs', type=str, default=None)
    ap.add_argument('--limit', type=int, default=None)
    args = ap.parse_args()
    if not s.SUPABASE_URL or not s.SUPABASE_KEY:
        sys.exit('Missing SUPABASE creds in .env')

    print('Loading DB...')
    fights = fetch_all('fights', 'id,event_id,fighter1_id,fighter2_id,is_amateur')
    by_fighter = {}   # fighter_id -> [(fight_id, event_id)]
    for f in fights:
        for fid in (f['fighter1_id'], f['fighter2_id']):
            if fid:
                by_fighter.setdefault(fid, []).append((f['id'], f['event_id']))

    fighter_ids = set(by_fighter)
    if args.orgs:
        wanted = {o.strip().lower() for o in args.orgs.split(',')}
        events = fetch_all('events', 'id,organization')
        org_ev = {e['id'] for e in events if (e.get('organization') or '').lower() in wanted}
        fighter_ids = {fid for fid, lst in by_fighter.items()
                       if any(ev in org_ev for _fid, ev in lst)}
        print(f'  scoped to orgs {sorted(wanted)}: {len(fighter_ids)} fighters')

    checked = set()
    if os.path.exists(CHECKED_FILE):
        with open(CHECKED_FILE) as fh:
            checked = {int(x) for x in fh.read().split() if x.strip().isdigit()}
    todo = [fid for fid in fighter_ids if fid not in checked]
    if args.limit:
        todo = todo[:args.limit]
    print(f'  {len(todo)} fighters to check ({len(checked)} already done)\n')

    to_flag = set()
    for i, fid in enumerate(todo):
        try:
            am_events = amateur_event_ids(fid)
        except Exception as e:
            print(f'  [WARN] fighter {fid}: {e}')
            continue
        if am_events:
            for fight_id, event_id in by_fighter[fid]:
                if event_id in am_events:
                    to_flag.add(fight_id)
        with open(CHECKED_FILE, 'a') as fh:
            fh.write(f'{fid}\n')
        if (i + 1) % 200 == 0:
            print(f'  ...{i+1}/{len(todo)}  ({len(to_flag)} fights to flag so far)')
        time.sleep(0.3)

    print(f'\nFlagging {len(to_flag)} amateur fights...')
    ids = list(to_flag)
    for j in range(0, len(ids), 100):
        chunk = ids[j:j+100]
        inlist = ','.join(f'"{c}"' for c in chunk)   # fight ids are text
        r = requests.patch(f'{s.SUPABASE_URL}/rest/v1/fights?id=in.({inlist})',
                           headers=s._headers(), data=json.dumps({'is_amateur': True}))
        if r.status_code not in (200, 204):
            sys.exit(f'  flag failed ({r.status_code}): {r.text[:200]}')
    print(f'Done — flagged {len(to_flag)} fights amateur.')


if __name__ == '__main__':
    main()
