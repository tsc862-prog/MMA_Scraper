#!/usr/bin/env python3
"""
gender_scraper.py
Resolves fighter gender via Wikidata for non-UFC fighters that aren't already
known female. Sherdog doesn't gender-label weight classes, and graph
propagation can't reach isolated women's clusters — Wikidata's "sex or gender"
property (P21) fills those gaps.

Only ONE fighter per fight needs resolving: once a woman is flagged,
recalculate_womens_divisions() propagates female-ness to her opponents and
relabels the weight classes.

  python gender_scraper.py            # check unchecked non-UFC fighters
  python gender_scraper.py --all      # re-check everyone (ignores the flag)
"""

import os
import re
import sys
import json
import time
import argparse
import requests
import gender_guesser.detector as gender_detector

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

WIKIDATA_API = 'https://www.wikidata.org/w/api.php'
WIKIDATA_ENTITY = 'https://www.wikidata.org/wiki/Special:EntityData/{}.json'
# Wikidata throttles non-compliant UAs — policy wants app + contact.
USER_AGENT = ('MMAScraper/1.0 '
              '(https://github.com/tsc862-prog/MMA_Scraper; tsc862@gmail.com) '
              'python-requests')

# Wikidata: P21 sex-or-gender; Q6581072 female, Q6581097 male.
Q_FEMALE = 'Q6581072'
Q_MALE   = 'Q6581097'

# A matched entity must look like a combat-sports athlete.
COMBAT_RE = re.compile(
    r'mixed martial|martial artist|\bMMA\b|kickbox|\bboxer\b|\bfighter\b|grappler',
    re.IGNORECASE)

# Offline first-name → gender guesser (triage signal only, not authoritative).
_NAME_DETECTOR = gender_detector.Detector(case_sensitive=False)

sys.stdout.reconfigure(encoding='utf-8')

def name_looks_female(full_name):
    """True if the fighter's first name guesses female."""
    if not full_name or not full_name.strip():
        return False
    first = full_name.strip().split()[0]
    return _NAME_DETECTOR.get_gender(first) in ('female', 'mostly_female')

# ── Supabase helpers ──────────────────────────────────────────────────────────

def _sb_headers():
    return {
        'apikey': SUPABASE_KEY,
        'Authorization': f'Bearer {SUPABASE_KEY}',
        'Content-Type': 'application/json',
        'Prefer': 'return=minimal',
    }

def fetch_all(table, select='*', extra=''):
    PAGE, offset, out = 1000, 0, []
    while True:
        r = requests.get(
            f'{SUPABASE_URL}/rest/v1/{table}'
            f'?select={select}&limit={PAGE}&offset={offset}{extra}',
            headers=_sb_headers())
        if not r.ok:
            print(f'  [WARN] fetch {table} failed ({r.status_code})')
            break
        batch = r.json()
        out.extend(batch)
        if len(batch) < PAGE:
            break
        offset += PAGE
    return out

def patch_fighter(fighter_id, payload):
    r = requests.patch(
        f'{SUPABASE_URL}/rest/v1/fighters?id=eq.{fighter_id}',
        headers=_sb_headers(), data=json.dumps(payload))
    return r.status_code in (200, 204)

def rpc(name):
    r = requests.post(f'{SUPABASE_URL}/rest/v1/rpc/{name}',
                      headers=_sb_headers(), data='{}')
    if r.status_code not in (200, 204):
        print(f'  [WARN] rpc {name} failed ({r.status_code}): {r.text[:200]}')

def insert_review_items(items):
    if not items:
        return
    r = requests.post(f'{SUPABASE_URL}/rest/v1/review_queue',
                      headers=_sb_headers(), data=json.dumps(items))
    if r.status_code not in (200, 201):
        print(f'  [WARN] review_queue insert failed ({r.status_code}): {r.text[:200]}')

def patch_review_item(item_id, payload):
    requests.patch(f'{SUPABASE_URL}/rest/v1/review_queue?id=eq.{item_id}',
                   headers=_sb_headers(), data=json.dumps(payload))

# ── Wikidata ──────────────────────────────────────────────────────────────────

def _wd_request(url, params):
    """GET Wikidata JSON with retry/backoff. Returns parsed JSON or None."""
    for attempt in range(4):
        try:
            r = requests.get(url, params=params,
                             headers={'User-Agent': USER_AGENT}, timeout=25)
            if r.status_code == 200:
                try:
                    return r.json()
                except ValueError:
                    pass   # non-JSON body = throttle/error page
        except requests.RequestException:
            pass
        time.sleep(2 ** attempt)   # 1, 2, 4, 8 s
    return None

def wikidata_gender(name):
    """
    Look up a fighter on Wikidata.
    Returns (status, gender): status 'ok' (definitive) or 'error' (retry later);
    gender 'F', 'M', or None.
    """
    search = _wd_request(WIKIDATA_API, {
        'action': 'wbsearchentities', 'search': name,
        'language': 'en', 'type': 'item', 'limit': 7, 'format': 'json',
    })
    if search is None:
        return 'error', None

    entity_id = None
    for r in search.get('search', []):
        if COMBAT_RE.search(r.get('description', '')):
            entity_id = r['id']
            break
    if not entity_id:
        return 'ok', None   # no combat-sports entity on Wikidata

    entity = _wd_request(WIKIDATA_ENTITY.format(entity_id), None)
    if entity is None:
        return 'error', None
    try:
        claims = entity['entities'][entity_id].get('claims', {})
        p21 = claims.get('P21')
        if not p21:
            return 'ok', None
        gid = p21[0]['mainsnak']['datavalue']['value']['id']
        if gid == Q_FEMALE:
            return 'ok', 'F'
        if gid == Q_MALE:
            return 'ok', 'M'
    except (KeyError, IndexError, TypeError):
        pass
    return 'ok', None

# ── Main ──────────────────────────────────────────────────────────────────────

def main(check_all=False):
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise ValueError('Missing SUPABASE_URL or SUPABASE_KEY in .env')

    print('Loading fighters / fights / events...')
    fighters = fetch_all('fighters', 'id,name,gender,gender_checked')
    events   = fetch_all('events', 'id,organization')
    fights   = fetch_all('fights', 'fighter1_id,fighter2_id,event_id')

    non_ufc_event_ids = {e['id'] for e in events if e['organization'] != 'UFC'}
    non_ufc_fighter_ids = set()
    for f in fights:
        if f['event_id'] in non_ufc_event_ids:
            non_ufc_fighter_ids.add(f['fighter1_id'])
            non_ufc_fighter_ids.add(f['fighter2_id'])

    # Candidates: non-UFC fighters not already known female.
    # UFC-only fighters are reliably gendered by ufcstats labels — skip them.
    candidates = [
        f for f in fighters
        if f['id'] in non_ufc_fighter_ids
        and f.get('gender') != 'F'
        and (check_all or not f.get('gender_checked'))
    ]
    print(f'  {len(candidates)} fighters to check against Wikidata\n')

    found_female = []
    errors = consecutive_errors = 0
    for i, f in enumerate(candidates):
        if (i + 1) % 50 == 0:
            print(f'  ...{i+1}/{len(candidates)}  '
                  f'(female={len(found_female)}, errors={errors})')

        status, gender = wikidata_gender(f['name'])

        if status == 'error':
            errors += 1
            consecutive_errors += 1
            if consecutive_errors >= 25:
                print('\n[ABORT] 25 consecutive Wikidata errors — stopping. '
                      'Unchecked fighters will be retried on the next run.')
                break
            continue   # leave gender_checked false → retried next run
        consecutive_errors = 0

        payload = {'gender_checked': True}
        if gender == 'F':
            payload['gender'] = 'F'
            found_female.append(f['name'])
            print(f'  FEMALE: {f["name"]}')
        patch_fighter(f['id'], payload)
        time.sleep(0.4)   # be polite to Wikidata

    print(f'\nWikidata confirmed {len(found_female)} female fighters '
          f'({errors} lookups errored, will retry next run).')
    if found_female:
        print('Recalculating women\'s divisions (propagates to opponents)...')
        rpc('recalculate_womens_divisions')

    triage_ambiguous_fights()
    print('Done.')


def triage_ambiguous_fights():
    """
    Flag non-UFC fights that look like they might be women's but weren't
    confidently labeled — into review_queue for manual M/F selection.
    A fight is flagged when a fighter's first name guesses female yet the
    fight isn't labeled "Women's" (Wikidata + propagation missed it).
    """
    print('\nTriaging unresolved fights with the name-guesser...')
    events   = fetch_all('events', 'id,name,organization')
    fights   = fetch_all('fights', 'id,event_id,fighter1_id,fighter2_id,weight_class')
    fighters = fetch_all('fighters', 'id,name')
    name_by_id = {f['id']: f['name'] for f in fighters}
    ev_by_id   = {e['id']: e for e in events}

    # Every gender review item ever created, by fight_id.
    resp = requests.get(
        f'{SUPABASE_URL}/rest/v1/review_queue'
        f'?type=eq.gender&select=id,subject_id,status',
        headers=_sb_headers())
    all_items = resp.json() if resp.ok else []
    pending  = {x['subject_id']: x['id'] for x in all_items if x['status'] == 'pending'}
    handled  = {x['subject_id'] for x in all_items}   # any status — never re-ask

    new_items = []
    for f in fights:
        ev = ev_by_id.get(f['event_id'])
        if not ev or ev['organization'] == 'UFC':
            continue
        wc = (f.get('weight_class') or '')
        labelled_womens = wc.lower().startswith('women')

        # Propagation already resolved a previously-queued fight → close it.
        if labelled_womens and f['id'] in pending:
            patch_review_item(pending[f['id']],
                              {'status': 'resolved',
                               'resolution': {'gender': 'F', 'by': 'propagation'}})
            continue
        # Skip if labelled women's, or this fight was already queued/answered.
        if labelled_womens or f['id'] in handled:
            continue

        n1 = name_by_id.get(f['fighter1_id'])
        n2 = name_by_id.get(f['fighter2_id'])
        if not n1 or not n2:
            continue
        if not (name_looks_female(n1) or name_looks_female(n2)):
            continue

        new_items.append({
            'type':       'gender',
            'subject_id': f['id'],
            'payload': {
                'fight_id':     f['id'],
                'fighter1_id':  f['fighter1_id'], 'fighter1_name': n1,
                'fighter2_id':  f['fighter2_id'], 'fighter2_name': n2,
                'event_name':   ev['name'],
            },
        })

    insert_review_items(new_items)
    print(f'  Queued {len(new_items)} fight(s) for gender review')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--all', action='store_true',
                        help='Re-check every fighter, ignoring gender_checked')
    args = parser.parse_args()
    main(check_all=args.all)
