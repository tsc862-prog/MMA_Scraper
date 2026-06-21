#!/usr/bin/env python3
"""
restore_video_links.py — re-apply video links wiped by the in-place cutover.

The *_url columns on events/fights (Paramount/ESPN/YouTube/Fight Pass, Netflix)
were populated by the video scrapers and got truncated during 01_prepare. The
pre-cutover backup captured them, so re-map them onto the new Sherdog-keyed rows
by natural key (event name+date; fight = fighter pair + event), reusing the
repoint matcher. Then the video scrapers keep accreting as before.

Usage (against PROD, .env):
  python migrate/restore_video_links.py <backup_dir>            # dry-run
  python migrate/restore_video_links.py <backup_dir> --apply
e.g. backup_dir = migrate/backup/20260621-065434
"""
import os
import sys
import json
import argparse
from collections import defaultdict

sys.path.insert(0, os.path.dirname(__file__))           # for repoint_ratings
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import requests
import import_ratings as ir
import repoint_ratings as rr   # name_tokens, norm_date, _date_window, match_fight

EVENT_URL_COLS = ['paramount_url', 'espn_url', 'espn_prelims_url', 'youtube_url',
                  'netflix_url', 'fightpass_url', 'fightpass_prelims_url',
                  'youtube_prelims_url']
FIGHT_URL_COLS = ['paramount_url', 'youtube_url', 'fightpass_url']


def load_jsonl(path):
    with open(path, encoding='utf-8') as fh:
        return [json.loads(line) for line in fh if line.strip()]


def match_event(name, date, elk):
    el = (name or '').lower().strip()
    if el in elk['by_name']:
        return elk['by_name'][el]
    et = rr.name_tokens(name)
    best, bs, tie = None, 0, False
    for d in rr._date_window(date):
        for e in elk['by_date'].get(d, []):
            s = len(rr.name_tokens(e['name']) & et)
            if s > bs:
                best, bs, tie = e, s, False
            elif s == bs and best and e['id'] != best['id']:
                tie = True
    if best and bs >= 2 and not tie:
        return best['id']
    return None


def upsert(table, rows):
    for i in range(0, len(rows), 500):
        b = rows[i:i+500]
        r = requests.post(f'{ir.SUPABASE_URL}/rest/v1/{table}',
                          headers={**ir._headers(), 'Prefer': 'resolution=merge-duplicates,return=minimal'},
                          data=json.dumps(b))
        if r.status_code not in (200, 201, 204):
            sys.exit(f'upsert {table} failed ({r.status_code}): {r.text[:300]}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('backup_dir')
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()
    if not ir.SUPABASE_URL or not ir.SUPABASE_KEY:
        sys.exit('Missing creds')

    print('Loading NEW prod...')
    n_fighters = ir.fetch_all('fighters', 'id,name')
    n_aliases = ir.fetch_all('fighter_aliases', 'alias,fighter_id')
    n_events = ir.fetch_all('events', 'id,name,date')
    n_fights = ir.fetch_all('fights', 'id,event_id,fighter1_id,fighter2_id')

    # --- fight matcher lk (mirror repoint_ratings.main) ---
    name_ids, alias_ids, last_ids = defaultdict(list), defaultdict(list), defaultdict(list)
    for f in n_fighters:
        if f.get('name'):
            name_ids[f['name'].lower().strip()].append(f['id'])
            last_ids[f['name'].split()[-1].lower()].append(f['id'])
    for al in n_aliases:
        alias_ids[al['alias'].lower().strip()].append(al['fighter_id'])

    def resolve(name):
        nl = (name or '').lower().strip()
        if name_ids.get(nl):
            return name_ids[nl]
        if alias_ids.get(nl):
            return alias_ids[nl]
        return last_ids.get(nl.split()[-1], []) if nl else []

    event_by_id = {e['id']: e for e in n_events}
    ev_by_name_lower = {e['name'].lower(): e['id'] for e in n_events if e.get('name')}
    fight_lookup, fights_by_event = defaultdict(list), defaultdict(list)
    for fi in n_fights:
        if fi['fighter1_id'] and fi['fighter2_id']:
            fight_lookup[frozenset((fi['fighter1_id'], fi['fighter2_id']))].append(fi)
        fights_by_event[fi['event_id']].append(fi)
    new_event_date = {e['id']: rr.norm_date(e.get('date')) for e in n_events}
    events_by_date = defaultdict(list)
    for e in n_events:
        events_by_date[rr.norm_date(e.get('date'))].append(e)
    new_name = {f['id']: f.get('name', '') for f in n_fighters}
    lk = {'resolve': resolve, 'event_by_id': event_by_id, 'ev_by_name_lower': ev_by_name_lower,
          'fight_lookup': fight_lookup, 'new_event_date': new_event_date,
          'fights_by_event': fights_by_event, 'events_by_date': events_by_date, 'new_name': new_name}
    elk = {'by_name': ev_by_name_lower, 'by_date': events_by_date}

    # --- OLD data from backup ---
    print('Loading backup...')
    o_fighters = {f['id']: f['name'] for f in load_jsonl(os.path.join(args.backup_dir, 'fighters.jsonl'))}
    o_events = load_jsonl(os.path.join(args.backup_dir, 'events.jsonl'))
    o_fights = load_jsonl(os.path.join(args.backup_dir, 'fights.jsonl'))

    def merge(dst, nid, src, cols):
        row = dst.setdefault(nid, {'id': nid, **{c: None for c in cols}})
        for c in cols:
            if src.get(c) and not row[c]:   # coalesce: first non-null wins
                row[c] = src[c]

    # --- events ---
    ev_map, ev_unmatched = {}, 0
    o_events_by_id = {e['id']: e for e in o_events}
    for e in o_events:
        if not any(e.get(c) for c in EVENT_URL_COLS):
            continue
        nid = match_event(e.get('name'), rr.norm_date(e.get('date')), elk)
        if nid:
            merge(ev_map, nid, e, EVENT_URL_COLS)
        else:
            ev_unmatched += 1

    # --- fights ---
    fi_map, fi_unmatched = {}, 0
    for f in o_fights:
        if not any(f.get(c) for c in FIGHT_URL_COLS):
            continue
        a = o_fighters.get(f['fighter1_id'])
        b = o_fighters.get(f['fighter2_id'])
        oe = o_events_by_id.get(f['event_id'], {})
        nid, _ = rr.match_fight(a, b, oe.get('name'), rr.norm_date(oe.get('date')), lk)
        if nid:
            merge(fi_map, nid, f, FIGHT_URL_COLS)
        else:
            fi_unmatched += 1

    # events.name is NOT NULL — include the new name so the upsert's insert
    # tuple is valid (it resolves to an UPDATE on conflict anyway).
    ev_updates = [{**row, 'name': event_by_id[row['id']]['name']} for row in ev_map.values()]
    # fights.event_id/fighter*_id are NOT NULL — include them so the upsert's
    # insert tuple is valid (resolves to UPDATE on conflict).
    new_fight_by_id = {f['id']: f for f in n_fights}
    fi_updates = [{**row,
                   'event_id': new_fight_by_id[row['id']]['event_id'],
                   'fighter1_id': new_fight_by_id[row['id']]['fighter1_id'],
                   'fighter2_id': new_fight_by_id[row['id']]['fighter2_id']}
                  for row in fi_map.values()]
    print(f'Events: {len(ev_updates)} to restore, {ev_unmatched} unmatched')
    print(f'Fights: {len(fi_updates)} to restore, {fi_unmatched} unmatched')

    if not args.apply:
        print('Dry run — re-run with --apply to write.')
        return

    print('Applying event links...'); upsert('events', ev_updates)
    print('Applying fight links...'); upsert('fights', fi_updates)
    print('Done.')


if __name__ == '__main__':
    main()
