#!/usr/bin/env python3
"""
import_ratings.py
Match personal fight ratings from a CSV against the Supabase fight DB
and optionally push them to a ratings table.

CSV columns: Event, Placement, Weight, Title?, Winner, Loser, Round, Time, Finish, Rating, Notes

Usage:
  python import_ratings.py "path/to/ratings.csv"          # match only, write txt
  python import_ratings.py "path/to/ratings.csv" --push   # also push to DB
"""

import os
import sys
import csv
import re
import json
import requests

# ── Load .env ─────────────────────────────────────────────────────────────────
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
OUTPUT_FILE  = 'ratings_import.txt'

# ── Supabase helpers ──────────────────────────────────────────────────────────

def _headers():
    return {
        'apikey':        SUPABASE_KEY,
        'Authorization': f'Bearer {SUPABASE_KEY}',
        'Content-Type':  'application/json',
        'Prefer':        'resolution=merge-duplicates,return=minimal',
    }

def fetch_all(table, select='*'):
    """Fetch all rows via pagination (1 000 rows per page)."""
    PAGE = 1000
    offset = 0
    results = []
    while True:
        url = (f'{SUPABASE_URL}/rest/v1/{table}'
               f'?select={select}&limit={PAGE}&offset={offset}')
        resp = requests.get(url, headers=_headers())
        if not resp.ok:
            print(f'  [WARN] fetch {table} failed ({resp.status_code}): {resp.text[:200]}')
            break
        batch = resp.json()
        results.extend(batch)
        if len(batch) < PAGE:
            break
        offset += PAGE
    return results

# ── Load DB ───────────────────────────────────────────────────────────────────

def load_db():
    print('Loading fighters...')
    fighters = fetch_all('fighters', 'id,name')
    print(f'  {len(fighters)} fighters')

    print('Loading aliases...')
    aliases = fetch_all('fighter_aliases', 'alias,fighter_id')
    print(f'  {len(aliases)} aliases')

    print('Loading events...')
    events = fetch_all('events', 'id,name,date')
    print(f'  {len(events)} events')

    print('Loading fights...')
    fights = fetch_all('fights', 'id,event_id,fighter1_id,fighter2_id')
    print(f'  {len(fights)} fights')

    return fighters, aliases, events, fights

# ── Build lookup tables ───────────────────────────────────────────────────────

def build_lookups(fighters, aliases, events, fights):
    fighter_by_name  = {}   # name.lower()  -> fighter_id
    fighter_by_last  = {}   # last.lower()  -> [fighter_id, ...]
    fighter_by_alias = {}   # alias.lower() -> fighter_id
    fighter_names    = {}   # fighter_id    -> display name

    for f in fighters:
        fid, name = f['id'], f['name']
        fighter_names[fid] = name
        fighter_by_name[name.lower()] = fid
        last = name.split()[-1].lower()
        fighter_by_last.setdefault(last, []).append(fid)

    for a in aliases:
        fighter_by_alias[a['alias'].lower()] = a['fighter_id']

    event_by_id          = {e['id']: e for e in events}
    event_by_name_lower  = {e['name'].lower(): e['id'] for e in events}

    # Prefix index: "ufc 246" -> [event_id, ...]  (for numbered events)
    event_by_prefix = {}
    for e in events:
        m = re.match(r'^(ufc\s+\d+)\b', e['name'], re.IGNORECASE)
        if m:
            prefix = m.group(1).lower()
            event_by_prefix.setdefault(prefix, []).append(e['id'])

    # fight_lookup: frozenset({f1_id, f2_id}) -> [fight dict, ...]
    fight_lookup = {}
    for f in fights:
        if f['fighter1_id'] and f['fighter2_id']:
            key = frozenset((f['fighter1_id'], f['fighter2_id']))
            fight_lookup.setdefault(key, []).append(f)

    return (fighter_by_name, fighter_by_last, fighter_by_alias,
            fighter_names, event_by_id, event_by_name_lower,
            event_by_prefix, fight_lookup)

# ── Fighter resolution ────────────────────────────────────────────────────────

def resolve_fighter(name, fighter_by_name, fighter_by_alias, fighter_by_last):
    """Full name -> alias -> last-name fallback. Returns list of candidate IDs."""
    nl = name.lower().strip()
    fid = fighter_by_name.get(nl)
    if fid:
        return [fid], 'exact'
    fid = fighter_by_alias.get(nl)
    if fid:
        return [fid], 'alias'
    last = nl.split()[-1]
    ids = fighter_by_last.get(last, [])
    return ids, 'last_name' if ids else 'not_found'

# ── Fight matching ────────────────────────────────────────────────────────────

def find_fight_for_row(winner, loser, csv_event,
                       fighter_by_name, fighter_by_alias, fighter_by_last,
                       event_by_id, event_by_name_lower, event_by_prefix,
                       fight_lookup, fighter_names):
    """
    Returns (fight_id, match_note) or (None, reason_string).
    """
    winner_ids, wmatch = resolve_fighter(winner, fighter_by_name, fighter_by_alias, fighter_by_last)
    loser_ids,  lmatch = resolve_fighter(loser,  fighter_by_name, fighter_by_alias, fighter_by_last)

    if not winner_ids:
        return None, f'winner not found: "{winner}"'
    if not loser_ids:
        return None, f'loser not found: "{loser}"'

    # Gather all candidate fights
    candidates = []
    for wid in winner_ids:
        for lid in loser_ids:
            key = frozenset((wid, lid))
            for fight in fight_lookup.get(key, []):
                candidates.append(fight)

    if not candidates:
        return None, f'no fight in DB for {winner} vs {loser}'

    def fight_label(f):
        n1 = fighter_names.get(f['fighter1_id'], '?')
        n2 = fighter_names.get(f['fighter2_id'], '?')
        ev = event_by_id.get(f['event_id'], {})
        return f'{n1} vs {n2} @ {ev.get("name", "?")}'

    if len(candidates) == 1:
        f = candidates[0]
        return f['id'], f'matched ({wmatch}/{lmatch}): {fight_label(f)}'

    # ── Multiple candidates → disambiguate by event ───────────────────────────
    csv_ev_lower = csv_event.lower().strip()

    # 1) Exact event name match
    ev_id = event_by_name_lower.get(csv_ev_lower)
    if ev_id:
        hits = [c for c in candidates if c['event_id'] == ev_id]
        if len(hits) == 1:
            return hits[0]['id'], f'matched by exact event: {fight_label(hits[0])}'
        if len(hits) > 1:
            return None, f'still ambiguous after exact event match: {winner} vs {loser} @ {csv_event}'

    # 2) Numbered UFC prefix  e.g. "ufc 246"
    m = re.match(r'^(ufc\s+\d+)\b', csv_ev_lower)
    if m:
        prefix = m.group(1)
        ev_ids = set(event_by_prefix.get(prefix, []))
        hits = [c for c in candidates if c['event_id'] in ev_ids]
        if len(hits) == 1:
            return hits[0]['id'], f'matched by UFC# prefix: {fight_label(hits[0])}'
        if len(hits) > 1:
            return None, f'still ambiguous after UFC# match: {winner} vs {loser} @ {csv_event}'

    # 3) Partial name match — CSV event name contains DB event name or vice-versa
    partial = []
    for c in candidates:
        ev = event_by_id.get(c['event_id'], {})
        ev_name_lower = ev.get('name', '').lower()
        if ev_name_lower and (ev_name_lower in csv_ev_lower or csv_ev_lower in ev_name_lower):
            partial.append(c)

    if len(partial) == 1:
        return partial[0]['id'], f'matched by partial event: {fight_label(partial[0])}'
    if len(partial) > 1:
        return None, (f'ambiguous ({len(partial)} candidates after partial event): '
                      f'{winner} vs {loser} @ {csv_event}')

    # 4) Normalized match — strip punctuation + extra spaces from both sides
    def _norm(s):
        return re.sub(r'[^\w\s]', '', s.lower()).split()
    csv_words = set(_norm(csv_ev_lower))
    norm_hits = []
    for c in candidates:
        ev = event_by_id.get(c['event_id'], {})
        ev_words = set(_norm(ev.get('name', '')))
        # Require at least 80% word overlap (handles minor typos like "NIght" vs "night")
        if ev_words and len(csv_words & ev_words) / max(len(csv_words), len(ev_words)) >= 0.8:
            norm_hits.append(c)
    if len(norm_hits) == 1:
        return norm_hits[0]['id'], f'matched by normalized event: {fight_label(norm_hits[0])}'
    if len(norm_hits) > 1:
        # Tie-break: prefer event whose type (fight night vs numbered) matches CSV
        csv_is_fn = 'fight night' in csv_ev_lower
        typed = [c for c in norm_hits
                 if ('fight night' in event_by_id.get(c['event_id'], {}).get('name', '').lower()) == csv_is_fn]
        if len(typed) == 1:
            return typed[0]['id'], f'matched by normalized+type event: {fight_label(typed[0])}'

    # 5) Keyword match — extract fighter names from "Event: A vs B" title and
    #    check if both appear as substrings in any DB event name.
    #    Handles typos like "Emmet" matching "Emmett", "FIght Night 271" matching
    #    "Fight Night: Adesanya", etc.
    mk = re.search(r':\s*([^:]+?)\s+vs\.?\s+(.+)', csv_ev_lower, re.IGNORECASE)
    if mk:
        kw1 = mk.group(1).strip()
        kw2 = mk.group(2).strip()
        kw_hits = []
        for c in candidates:
            ev = event_by_id.get(c['event_id'], {})
            ev_lower = ev.get('name', '').lower()
            if kw1 in ev_lower and kw2 in ev_lower:
                kw_hits.append(c)
        if len(kw_hits) == 1:
            return kw_hits[0]['id'], f'matched by event keywords ({kw1}/{kw2}): {fight_label(kw_hits[0])}'
        if len(kw_hits) > 1:
            # Tie-break by fight-night vs numbered
            csv_is_fn = 'fight night' in csv_ev_lower
            typed = [c for c in kw_hits
                     if ('fight night' in event_by_id.get(c['event_id'], {}).get('name', '').lower()) == csv_is_fn]
            if len(typed) == 1:
                return typed[0]['id'], f'matched by keywords+type: {fight_label(typed[0])}'
            return None, (f'ambiguous after keyword match: '
                          f'{winner} vs {loser} @ {csv_event}')

    return None, (f'ambiguous ({len(candidates)} candidates, no event match): '
                  f'{winner} vs {loser} @ {csv_event}')

# ── DB push ───────────────────────────────────────────────────────────────────

def push_ratings(matched_rows):
    import time
    now_ms = int(time.time() * 1000)
    records = []
    for row in matched_rows:
        rec = {
            'fight_id':  row['fight_id'],
            'rating':    float(row['rating']),
            'logged_at': now_ms,
        }
        if row.get('notes'):
            rec['notes'] = row['notes']
        records.append(rec)

    PAGE = 500
    pushed = 0
    for i in range(0, len(records), PAGE):
        batch = records[i:i+PAGE]
        resp = requests.post(
            f'{SUPABASE_URL}/rest/v1/ratings',
            headers=_headers(),
            data=json.dumps(batch),
        )
        if resp.status_code not in (200, 201):
            print(f'  [WARN] push failed ({resp.status_code}): {resp.text[:200]}')
        else:
            pushed += len(batch)
            print(f'  pushed {pushed}/{len(records)}')

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description='Import fight ratings from CSV')
    parser.add_argument('csv_file', help='Path to ratings CSV')
    parser.add_argument('--push', action='store_true',
                        help='Push matched ratings to Supabase fight_ratings table')
    args = parser.parse_args()

    if not os.path.exists(args.csv_file):
        print(f'File not found: {args.csv_file}')
        sys.exit(1)

    if not SUPABASE_URL or not SUPABASE_KEY:
        print('Missing SUPABASE_URL or SUPABASE_KEY in .env')
        sys.exit(1)

    fighters, aliases, events, fights = load_db()
    (fighter_by_name, fighter_by_last, fighter_by_alias,
     fighter_names, event_by_id, event_by_name_lower,
     event_by_prefix, fight_lookup) = build_lookups(fighters, aliases, events, fights)

    # ── Parse CSV ─────────────────────────────────────────────────────────────
    rows = []
    with open(args.csv_file, newline='', encoding='utf-8-sig') as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            winner = row.get('Winner', '').strip()
            loser  = row.get('Loser',  '').strip()
            if not winner or not loser:
                continue
            rows.append(row)

    print(f'\nMatching {len(rows)} fights...\n')

    matched   = []
    unmatched = []

    for row in rows:
        winner    = row['Winner'].strip()
        loser     = row['Loser'].strip()
        csv_event = row['Event'].strip()
        rating    = row.get('Rating', '').strip()
        notes     = row.get('Notes', '').strip()

        fight_id, match_note = find_fight_for_row(
            winner, loser, csv_event,
            fighter_by_name, fighter_by_alias, fighter_by_last,
            event_by_id, event_by_name_lower, event_by_prefix,
            fight_lookup, fighter_names,
        )

        entry = {
            'csv_event': csv_event,
            'placement': row.get('Placement', ''),
            'weight':    row.get('Weight',    ''),
            'winner':    winner,
            'loser':     loser,
            'rating':    rating,
            'notes':     notes,
            'fight_id':  fight_id,
            'match_note': match_note,
        }

        if fight_id:
            matched.append(entry)
        else:
            unmatched.append(entry)

    # ── Write results ─────────────────────────────────────────────────────────
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as out:
        out.write('=== RATINGS IMPORT RESULTS ===\n')
        out.write(f'Total: {len(rows)}  |  Matched: {len(matched)}  |  Unmatched: {len(unmatched)}\n\n')

        out.write(f'─── MATCHED ({len(matched)}) ───\n\n')
        for e in matched:
            out.write(f'fight_id : {e["fight_id"]}\n')
            out.write(f'event    : {e["csv_event"]} | {e["placement"]} | {e["weight"]}\n')
            out.write(f'fighters : {e["winner"]} vs {e["loser"]}\n')
            out.write(f'rating   : {e["rating"]}\n')
            if e['notes']:
                out.write(f'notes    : {e["notes"]}\n')
            out.write(f'match    : {e["match_note"]}\n')
            out.write('\n')

        out.write(f'─── UNMATCHED ({len(unmatched)}) ───\n\n')
        for e in unmatched:
            out.write(f'event    : {e["csv_event"]} | {e["placement"]} | {e["weight"]}\n')
            out.write(f'fighters : {e["winner"]} vs {e["loser"]}\n')
            out.write(f'rating   : {e["rating"]}\n')
            out.write(f'REASON   : {e["match_note"]}\n')
            out.write('\n')

    print(f'Matched   : {len(matched)}')
    print(f'Unmatched : {len(unmatched)}')
    print(f'Results   : {OUTPUT_FILE}')

    if args.push:
        if matched:
            print(f'\nPushing {len(matched)} ratings to Supabase...')
            push_ratings(matched)
        else:
            print('\nNothing to push.')
    else:
        print('\n(Pass --push to write matched ratings to the database)')

if __name__ == '__main__':
    main()
