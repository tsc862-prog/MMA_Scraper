#!/usr/bin/env python3
"""
repoint_ratings.py — zero-loss re-point of `ratings` onto Sherdog-keyed fights.

The irreplaceable `ratings` table points at OLD fight ids (ufcstats hex for UFC,
name-hash uuid5 for the Sherdog orgs). After the migration every fight is keyed
on Sherdog numeric ids, so every rated fight's id changes. This script rebuilds
ratings.fight_id by matching each rating's OLD fight (its fighter pair + event +
date) to the NEW Sherdog fight.

Runs against STAGING, where both sides are present:
  - OLD tables renamed to *_old: fighters_old, events_old, fights_old,
    fight_results_old. `ratings` is kept as-is (its FK to fights must be DROPPED
    before --apply and re-added pointing at the new fights afterwards — see the
    Part 3 migration SQL).
  - NEW tables (fighters / events / fights / fight_results) rebuilt by
    sherdog_scraper.py with Sherdog ids.

Matching reuses the proven resolver in import_ratings.py (resolve_fighter +
build_lookups). Candidates are gathered by fighter pair, then disambiguated by
event DATE first (reliable across sources; settles rematches and draws), then by
event name. Winner/loser is irrelevant to pairing, so draws/NCs match too.

HARD GATE: if ANY rating is unmatched, the report lists them and the script exits
nonzero WITHOUT writing. Cutover is forbidden until unmatched == 0 (resolve via
fighter_aliases, event-name fixes, or a hand-map).

Usage:
  python migrate/repoint_ratings.py            # dry-run report (default)
  python migrate/repoint_ratings.py --apply    # write ratings.fight_id (gated)
"""

import os
import re
import sys
import json
import argparse
import unicodedata
from datetime import datetime

# Reuse the resolver + REST helpers from the sibling project module.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import requests
import import_ratings as ir

OLD = {
    'fighters': 'fighters_old',
    'events':   'events_old',
    'fights':   'fights_old',
    'results':  'fight_results_old',
}


def norm_date(s):
    """Normalize ufcstats / sherdog date strings to ISO YYYY-MM-DD."""
    if not s:
        return None
    s = s.strip()
    m = re.match(r'(\d{4}-\d{2}-\d{2})', s)
    if m:
        return m.group(1)
    for fmt in ('%B %d, %Y', '%b %d, %Y', '%m/%d/%Y'):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return None


_SUFFIX = {'jr', 'sr', 'ii', 'iii', 'iv', 'v'}

def name_tokens(s):
    """Lowercased, accent-folded token set; suffixes (jr/sr/iii) dropped.
    Token SETS make name order irrelevant ('Zhang Weili' == 'Weili Zhang')."""
    s = unicodedata.normalize('NFKD', s or '')
    s = ''.join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"[^a-z0-9 ]", ' ', s)
    return {t for t in s.split() if t and t not in _SUFFIX}


def clean_toks(s):
    """Ordered, accent-folded tokens with name-suffixes (jr/sr/iii) dropped.
    Used for last-name fallback so 'Michael Aswell Jr.' resolves on 'aswell',
    not the bogus token 'jr'."""
    s = unicodedata.normalize('NFKD', s or '')
    s = ''.join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"[^a-z0-9 ]", ' ', s)
    return [t for t in s.split() if t and t not in _SUFFIX]


def _date_window(iso):
    """[iso-1, iso, iso+1] — Sherdog dates use event-local tz, so international
    cards land ±1 day off ufcstats."""
    if not iso:
        return []
    try:
        d = datetime.strptime(iso, '%Y-%m-%d').date()
    except ValueError:
        return [iso]
    from datetime import timedelta
    return [(d + timedelta(days=k)).isoformat() for k in (0, -1, 1)]


def event_anchored(a, b, ev_name, ev_date, lk):
    """Fallback: the event date + the resolvable fighter pin the fight even when
    one side's spelling differs (nicknames, Chinese name order, romanization)."""
    evs = []
    for d in _date_window(ev_date):
        evs += lk['events_by_date'].get(d, [])
    if not evs:
        return None
    # When several events fall in the window, keep the best name-token match.
    if len(evs) > 1 and ev_name:
        et = name_tokens(ev_name)
        scored = [(len(name_tokens(e.get('name', '')) & et), e) for e in evs]
        mx = max(s for s, _ in scored)
        if mx > 0:
            evs = [e for s, e in scored if s == mx]

    ta, tb = name_tokens(a), name_tokens(b)
    best, best_score, tie = None, 0, False
    for e in evs:
        for fi in lk['fights_by_event'].get(e['id'], []):
            n1 = name_tokens(lk['new_name'].get(fi['fighter1_id'], ''))
            n2 = name_tokens(lk['new_name'].get(fi['fighter2_id'], ''))
            score = max(len(ta & n1) + len(tb & n2),
                        len(ta & n2) + len(tb & n1))
            if score > best_score:
                best, best_score, tie = fi, score, False
            elif score == best_score and best and fi['id'] != best['id']:
                tie = True
    # Require a clear, unambiguous winner with real token overlap.
    if best and best_score >= 2 and not tie:
        return best['id']
    return None


def match_fight(a, b, ev_name, ev_date, lk):
    """Resolve OLD (fighter a, fighter b, event, date) -> NEW fight id.
    Returns (new_fight_id, note) or (None, reason)."""
    resolve = lk['resolve']
    event_by_id = lk['event_by_id']
    ev_by_name_lower = lk['ev_by_name_lower']
    fight_lookup = lk['fight_lookup']
    new_event_date = lk['new_event_date']

    ids_a = resolve(a)
    ids_b = resolve(b)
    cands, seen = [], set()
    for x in ids_a:
        for y in ids_b:
            for c in fight_lookup.get(frozenset((x, y)), []):
                if c['id'] not in seen:
                    seen.add(c['id'])
                    cands.append(c)

    if len(cands) == 1:
        return cands[0]['id'], 'pair-unique'

    if len(cands) > 1:
        # disambiguate by event date (handles rematches)
        if ev_date:
            dh = [c for c in cands if new_event_date.get(c['event_id']) == ev_date]
            if len(dh) == 1:
                return dh[0]['id'], 'pair+date'
            if len(dh) > 1:
                cands = dh
        el = (ev_name or '').lower().strip()
        exact = ev_by_name_lower.get(el)
        if exact:
            h = [c for c in cands if c['event_id'] == exact]
            if len(h) == 1:
                return h[0]['id'], 'pair+exact-event'
        partial = [c for c in cands
                   if (en := event_by_id.get(c['event_id'], {}).get('name', '').lower())
                   and (en in el or el in en)]
        if len(partial) == 1:
            return partial[0]['id'], 'pair+partial-event'

    # Fallback: event-anchored fuzzy match (cross-source name mismatches).
    fid = event_anchored(a, b, ev_name, ev_date, lk)
    if fid:
        return fid, 'event-anchored'

    if not ids_a:
        return None, f'fighter not found in new DB: "{a}"'
    if not ids_b:
        return None, f'fighter not found in new DB: "{b}"'
    if not cands:
        return None, 'no new fight for pair'
    return None, f'ambiguous ({len(cands)} new fights for pair)'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true',
                    help='Write ratings.fight_id (only if zero unmatched)')
    args = ap.parse_args()

    if not ir.SUPABASE_URL or not ir.SUPABASE_KEY:
        sys.exit('Missing SUPABASE_URL / SUPABASE_KEY in .env')

    # ── NEW side (Sherdog-keyed) ────────────────────────────────────────────
    print('Loading NEW (Sherdog) tables...')
    n_fighters = ir.fetch_all('fighters', 'id,name')
    n_aliases  = ir.fetch_all('fighter_aliases', 'alias,fighter_id')
    n_events   = ir.fetch_all('events', 'id,name,date')
    n_fights   = ir.fetch_all('fights', 'id,event_id,fighter1_id,fighter2_id')

    # Name resolution must return ALL ids for a name — with Sherdog ids,
    # different people legitimately share a name (e.g. multiple "Bruno Silva").
    from collections import defaultdict
    name_ids, clean_ids, alias_ids, last_ids = (defaultdict(list), defaultdict(list),
                                                defaultdict(list), defaultdict(list))
    for f in n_fighters:
        if f.get('name'):
            name_ids[f['name'].lower().strip()].append(f['id'])
            ct = clean_toks(f['name'])
            if ct:
                clean_ids[' '.join(ct)].append(f['id'])
                last_ids[ct[-1]].append(f['id'])
    for al in n_aliases:
        alias_ids[al['alias'].lower().strip()].append(al['fighter_id'])

    def resolve(name):
        nl = (name or '').lower().strip()
        if name_ids.get(nl):
            return name_ids[nl]
        if alias_ids.get(nl):
            return alias_ids[nl]
        ct = clean_toks(name)
        if not ct:
            return []
        if clean_ids.get(' '.join(ct)):
            return clean_ids[' '.join(ct)]
        return last_ids.get(ct[-1], [])

    event_by_id = {e['id']: e for e in n_events}
    ev_by_name_lower = {e['name'].lower(): e['id'] for e in n_events if e.get('name')}
    fight_lookup = defaultdict(list)
    fights_by_event = defaultdict(list)
    for fi in n_fights:
        if fi['fighter1_id'] and fi['fighter2_id']:
            fight_lookup[frozenset((fi['fighter1_id'], fi['fighter2_id']))].append(fi)
        fights_by_event[fi['event_id']].append(fi)
    new_event_date = {e['id']: norm_date(e.get('date')) for e in n_events}
    events_by_date = defaultdict(list)
    for e in n_events:
        events_by_date[norm_date(e.get('date'))].append(e)
    new_name = {f['id']: f.get('name', '') for f in n_fighters}
    lk = {
        'resolve': resolve, 'event_by_id': event_by_id,
        'ev_by_name_lower': ev_by_name_lower, 'fight_lookup': fight_lookup,
        'new_event_date': new_event_date, 'fights_by_event': fights_by_event,
        'events_by_date': events_by_date, 'new_name': new_name,
    }

    # ── OLD side (pre-migration) ────────────────────────────────────────────
    print('Loading OLD (*_old) tables...')
    o_name  = {f['id']: f['name'] for f in ir.fetch_all(OLD['fighters'], 'id,name')}
    o_event = {e['id']: e for e in ir.fetch_all(OLD['events'], 'id,name,date')}
    o_fight = {f['id']: f for f in ir.fetch_all(OLD['fights'],
               'id,event_id,fighter1_id,fighter2_id')}

    ratings = ir.fetch_all('ratings', 'id,fight_id')
    print(f'\nRe-pointing {len(ratings)} ratings...\n')

    matched, unmatched, notes = [], [], {}
    for r in ratings:
        of = o_fight.get(r['fight_id'])
        if not of:
            unmatched.append((r, 'OLD fight not found'))
            continue
        a = o_name.get(of['fighter1_id'])
        b = o_name.get(of['fighter2_id'])
        ev = o_event.get(of['event_id'], {})
        if not (a and b):
            unmatched.append((r, 'OLD fighter name missing'))
            continue
        new_fid, note = match_fight(a, b, ev.get('name'),
                                    norm_date(ev.get('date')), lk)
        if new_fid:
            matched.append((r, new_fid))
            notes[note] = notes.get(note, 0) + 1
        else:
            unmatched.append((r, f'{note}  [{a} vs {b} @ {ev.get("name")} {ev.get("date")}]'))

    # ── Report ──────────────────────────────────────────────────────────────
    print(f'Matched   : {len(matched)}')
    for k, v in sorted(notes.items()):
        print(f'    {k:24} {v}')
    print(f'Unmatched : {len(unmatched)}')
    for r, why in unmatched:
        print(f'    rating {r["id"]} (old fight {r["fight_id"]}): {why}')

    # ── Hard gate ─────────────────────────────────────────────────────────────
    if unmatched:
        print('\nABORT: unmatched ratings exist — refusing to write. '
              'Resolve via aliases / event fixes, then re-run.')
        sys.exit(1)

    if not args.apply:
        print('\nDry run OK — all ratings matched. Re-run with --apply to write.')
        return

    print(f'\nApplying {len(matched)} updates to ratings.fight_id...')
    for i, (r, new_fid) in enumerate(matched):
        resp = requests.patch(
            f'{ir.SUPABASE_URL}/rest/v1/ratings?id=eq.{r["id"]}',
            headers=ir._headers(),
            data=json.dumps({'fight_id': new_fid}),
        )
        if resp.status_code not in (200, 204):
            sys.exit(f'  [FAIL] rating {r["id"]} ({resp.status_code}): {resp.text[:200]}')
        if (i + 1) % 100 == 0:
            print(f'  {i+1}/{len(matched)}')
    print(f'Done — re-pointed {len(matched)} ratings.')


if __name__ == '__main__':
    main()
