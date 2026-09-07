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

# Two tiers of org gender rules:
#
# LOCK orgs — single-gender promotions or all-male historical orgs. Every
# fighter who ever appeared at one of these is patched to that gender, then
# recalculate_womens_divisions() cascades it to their opponents in any
# other org (since MMA has no inter-gender bouts).
ORG_GENDER_LOCK = {
    'Invicta FC':       'F',
    'Vale Tudo Japan':  'M',
    'Affliction':       'M',
    'PRIDE':            'M',
    'WEC':              'M',
}
# REVIEW-DEFAULT orgs — predominantly one gender but with occasional bouts
# the other way. We auto-RESOLVE pending review_queue items to the default,
# but we DON'T patch the fighters' gender field. That way a future women's
# bout at one of these orgs (when surfaced by Sherdog tagging or by the
# cascade) still gets labeled correctly without our M-default clobbering it.
#
# Currently empty: every previously-considered candidate (Pancrase, Cage
# Warriors, CFFC, Road to UFC, LFA, RIZIN) is mixed enough that we'd
# rather see a manual confirmation than guess. The cascade from Invicta
# already catches the bulk of their women's fights via shared opponents.
ORG_REVIEW_DEFAULT = {}

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
    # order= is required: PostgREST offset paging without an ORDER BY is not
    # stable — rows get skipped/repeated between pages (2026-09-06 fix).
    order = 'alias' if table == 'fighter_aliases' else 'id'
    PAGE, offset, out = 1000, 0, []
    while True:
        r = requests.get(
            f'{SUPABASE_URL}/rest/v1/{table}'
            f'?select={select}&order={order}&limit={PAGE}&offset={offset}{extra}',
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

WIKIPEDIA_API = 'https://en.wikipedia.org/w/api.php'

def _wiki_request(params):
    """Wikipedia API call with retry/backoff. Same shape as _wd_request."""
    for attempt in range(3):
        try:
            r = requests.get(WIKIPEDIA_API,
                             params={**params, 'format': 'json'},
                             headers={'User-Agent': USER_AGENT}, timeout=20)
            if r.status_code == 200:
                try:
                    return r.json()
                except ValueError:
                    pass
        except requests.RequestException:
            pass
        time.sleep(2 ** attempt)
    return None

def wikipedia_gender(name):
    """Find a fighter's Wikipedia article and infer gender from pronouns
    in the lead paragraph. Conservative: requires a strong he/she ratio
    AND that the article itself looks like a combat-sports bio. Catches
    notable fighters whose Wikidata entry doesn't have P21 set.

    Returns (status, gender) — same shape as wikidata_gender."""
    search = _wiki_request({
        'action': 'query', 'list': 'search',
        'srsearch': f'{name} mixed martial arts',
        'srlimit': 5,
    })
    if search is None:
        return 'error', None

    # Pick the search hit whose title shares the most tokens with the
    # fighter's name. Falling back to last-name only caught the wrong
    # person when both share a surname (Amanda Nunes vs Nina Nunes).
    name_tokens = set(t for t in name.lower().split() if len(t) > 1)
    best_title, best_score = None, 0
    for hit in search.get('query', {}).get('search', []):
        title_tokens = set(hit['title'].lower().split())
        score = len(name_tokens & title_tokens)
        if score > best_score:
            best_score = score
            best_title = hit['title']
    # Require at least 2 name tokens to agree (or 1 for single-name fighters).
    if not best_title or best_score < min(2, len(name_tokens)):
        return 'ok', None
    title = best_title

    extract = _wiki_request({
        'action': 'query', 'prop': 'extracts',
        'titles': title, 'exintro': True, 'explaintext': True,
    })
    if extract is None:
        return 'error', None
    text = ''
    for page in extract.get('query', {}).get('pages', {}).values():
        text = page.get('extract', '') or ''
        break
    if not text:
        return 'ok', None
    # Only trust an article whose lead paragraph looks like a combat-sports bio
    # (otherwise we could pull pronouns from a coach, a movie character, etc.).
    if not COMBAT_RE.search(text[:600]):
        return 'ok', None
    he  = len(re.findall(r'\b[Hh]e\b|\b[Hh]is\b|\b[Hh]im\b', text))
    she = len(re.findall(r'\b[Ss]he\b|\b[Hh]er\b|\b[Hh]ers\b', text))
    # Require a strong signal so MMA bios that name many male opponents
    # don't swamp the count for a female subject.
    if she >= 3 and she > he * 2:
        return 'ok', 'F'
    if he >= 3 and he > she * 2:
        return 'ok', 'M'
    return 'ok', None

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

    # ── Pass 1: lock-org cascade (cheapest, most authoritative) ────────
    # Doing this BEFORE the Wikidata/Wikipedia loop means every fighter
    # the cascade reaches gets gender_checked=True and falls out of the
    # candidate list — sometimes 1000+ fewer API calls.
    print('Pass 1: lock-org propagation...')
    propagate_from_lock_orgs()

    # ── Pass 2: Wikidata + Wikipedia for whoever's left ────────────────
    print('\nLoading fighters / fights / events for Wikidata/Wikipedia pass...')
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
    print(f'  {len(candidates)} fighters to check against Wikidata/Wikipedia\n')

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

        # Wikidata says nothing definitive? Try Wikipedia pronoun scan as
        # a fallback. Errors there don't abort — Wikidata error is the
        # signal-of-throttling we care about.
        gender_source = 'wikidata' if gender else None
        if gender is None:
            _, gender = wikipedia_gender(f['name'])
            if gender:
                gender_source = 'wikipedia'
            time.sleep(0.4)

        payload = {'gender_checked': True}
        if gender == 'F':
            payload['gender'] = 'F'
            found_female.append(f['name'])
            print(f'  FEMALE [{gender_source}]: {f["name"]}')
        patch_fighter(f['id'], payload)
        time.sleep(0.4)   # be polite to Wikidata

    print(f'\nWikidata confirmed {len(found_female)} female fighters '
          f'({errors} lookups errored, will retry next run).')
    if found_female:
        print('Recalculating women\'s divisions (propagates to opponents)...')
        rpc('recalculate_womens_divisions')

    triage_ambiguous_fights()
    print('Done.')


def propagate_from_lock_orgs():
    """Every fighter who appeared at an event for an ORG_GENDER_LOCK org
    must be that gender. Bulk-patch, then cascade via
    recalculate_womens_divisions() (no inter-gender bouts exist, so the
    fighter's opponents across all orgs must be the same gender)."""
    if not ORG_GENDER_LOCK:
        return 0
    filt = '&organization=in.({})'.format(
        ','.join(o.replace(' ', '%20') for o in ORG_GENDER_LOCK))
    events = fetch_all('events', 'id,organization', filt)
    ev_to_org = {e['id']: e['organization'] for e in events}
    if not ev_to_org:
        return 0
    fights   = fetch_all('fights', 'fighter1_id,fighter2_id,event_id')
    fighters = fetch_all('fighters', 'id,gender')
    known    = {f['id']: f.get('gender') for f in fighters}

    # For each fighter, the gender implied by their lock-org appearances.
    # If a fighter shows up at both an F-lock org and an M-lock org we'd
    # have a data error — skip the M side (women's MMA is younger and
    # better-tagged; the F side is more trustworthy).
    target = {}
    for f in fights:
        org = ev_to_org.get(f['event_id'])
        if not org:
            continue
        g = ORG_GENDER_LOCK[org]
        for fid in (f['fighter1_id'], f['fighter2_id']):
            if not fid:
                continue
            if target.get(fid) == 'F':
                continue   # F-lock wins
            target[fid] = g
    # Drop fighters already correctly set.
    to_patch = {fid: g for fid, g in target.items() if known.get(fid) != g}
    # Don't downgrade a known F to M.
    to_patch = {fid: g for fid, g in to_patch.items()
                if not (known.get(fid) == 'F' and g == 'M')}
    if not to_patch:
        return 0
    counts = {'F': 0, 'M': 0}
    for fid, g in to_patch.items():
        patch_fighter(fid, {'gender': g, 'gender_checked': True})
        counts[g] += 1
    print(f'  Lock-org propagation: patched {len(to_patch)} fighters '
          f'(F={counts["F"]}, M={counts["M"]})')
    print('  Cascading to opponents via recalculate_womens_divisions...')
    rpc('recalculate_womens_divisions')
    return len(to_patch)


def triage_ambiguous_fights():
    """
    Flag non-UFC fights that look like they might be women's but weren't
    confidently labeled — into review_queue for manual M/F selection.
    A fight is flagged when a fighter's first name guesses female yet the
    fight isn't labeled "Women's" (Wikidata + propagation missed it).

    Drains the existing backlog before queuing new items:
      * ORG_REVIEW_DEFAULT — pending reviews at predominantly-single-
        gender orgs get resolved without touching fighter records (those
        orgs occasionally have other-gender bouts; we don't want to lock
        the fighter prematurely).
    Lock-org propagation runs in main() before this — no need to repeat.
    """
    print('\nTriaging unresolved fights with the name-guesser...')
    events   = fetch_all('events', 'id,name,organization')
    fights   = fetch_all('fights', 'id,event_id,fighter1_id,fighter2_id,weight_class')
    fighters = fetch_all('fighters', 'id,name,gender')
    name_by_id   = {f['id']: f['name']   for f in fighters}
    gender_by_id = {f['id']: f.get('gender') for f in fighters}
    ev_by_id     = {e['id']: e for e in events}

    # Every gender review item ever created, by fight_id.
    resp = requests.get(
        f'{SUPABASE_URL}/rest/v1/review_queue'
        f'?type=eq.gender&select=id,subject_id,status',
        headers=_sb_headers())
    all_items = resp.json() if resp.ok else []
    pending  = {x['subject_id']: x['id'] for x in all_items if x['status'] == 'pending'}
    handled  = {x['subject_id'] for x in all_items}   # any status — never re-ask

    # ── Drain existing backlog via ORG_REVIEW_DEFAULT ───────────────────
    # Lock-org reviews were already resolved (and fighters patched) by
    # propagate_from_lock_orgs() above. This pass handles the orgs where
    # we mark the review item resolved but DON'T patch the fighter.
    # Safety: skip if either fighter is already known F (almost certainly
    # a women's bout Sherdog forgot to tag).
    auto_resolved = 0
    for f in fights:
        if f['id'] not in pending:
            continue
        ev = ev_by_id.get(f['event_id'])
        if not ev:
            continue
        default = ORG_REVIEW_DEFAULT.get(ev['organization'])
        if not default:
            continue
        g1 = gender_by_id.get(f['fighter1_id'])
        g2 = gender_by_id.get(f['fighter2_id'])
        if default == 'M' and ('F' in (g1, g2)):
            continue   # leave for manual — almost certainly a women's bout
        patch_review_item(pending[f['id']],
                          {'status': 'resolved',
                           'resolution': {'gender': default, 'by': 'org_default'}})
        auto_resolved += 1
    if auto_resolved:
        print(f'  Auto-resolved {auto_resolved} pending reviews via ORG_REVIEW_DEFAULT '
              f'(no fighter records changed)')

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

        # Skip orgs that have a default — they'd just be auto-resolved next run.
        if (ev['organization'] in ORG_GENDER_LOCK
                or ev['organization'] in ORG_REVIEW_DEFAULT):
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

    if auto_resolved:
        print('  Recalculating women\'s divisions...')
        rpc('recalculate_womens_divisions')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--all', action='store_true',
                        help='Re-check every fighter, ignoring gender_checked')
    args = parser.parse_args()
    main(check_all=args.all)
