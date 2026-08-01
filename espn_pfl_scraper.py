#!/usr/bin/env python3
"""
espn_pfl_scraper.py
Fetches PFL event replay videos from ESPN's Watch catalog API and matches
them to PFL events in Supabase, storing the watch URL in events.espn_url.

ESPN has no per-fight video feed for PFL — only event-level replays
(Main Card / Prelims). This grabs the English Main-Card replay per event.

Results are written to espn_pfl_matches.txt for review.
Run with --push to write to the database.

  python espn_pfl_scraper.py            # dry run, writes txt only
  python espn_pfl_scraper.py --push     # also update events.espn_url
"""

import os
import re
import sys
import json
import argparse
import requests
from datetime import datetime

# Load .env
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

# PFL catalog GUID on ESPN's Watch API
PFL_CATALOG_ID = '1920b7fe-174b-43b7-8640-af9afcc5c255'
CATALOG_URL = ('https://watch-cdn.product.api.espn.com/api/product/v3'
               f'/watchespn/web/catalog/{PFL_CATALOG_ID}')
WATCH_URL_TMPL = 'https://www.espn.com/watch/player?id={}'
OUTPUT_FILE = 'espn_pfl_matches.txt'

USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36')

# ── Supabase helpers ──────────────────────────────────────────────────────────

def _sb_headers():
    return {
        'apikey': SUPABASE_KEY,
        'Authorization': f'Bearer {SUPABASE_KEY}',
        'Content-Type': 'application/json',
        'Prefer': 'return=minimal',
    }

def fetch_pfl_events():
    """All PFL events from Supabase."""
    resp = requests.get(
        f'{SUPABASE_URL}/rest/v1/events'
        f'?organization=eq.PFL&select=id,name,date&limit=2000',
        headers=_sb_headers(),
    )
    resp.raise_for_status()
    return resp.json()

def update_espn_urls(event_id, main_url, prelims_url):
    """Patch whichever of the two ESPN URLs were found for an event."""
    payload = {}
    if main_url:
        payload['espn_url'] = main_url
    if prelims_url:
        payload['espn_prelims_url'] = prelims_url
    if not payload:
        return False
    resp = requests.patch(
        f'{SUPABASE_URL}/rest/v1/events?id=eq.{event_id}',
        headers=_sb_headers(),
        data=json.dumps(payload),
    )
    return resp.status_code in (200, 204)

# ── ESPN catalog ──────────────────────────────────────────────────────────────

def fetch_catalog():
    """Return all video content items from the PFL Watch catalog."""
    resp = requests.get(CATALOG_URL, headers={'User-Agent': USER_AGENT}, timeout=30)
    resp.raise_for_status()
    items = []
    for bucket in resp.json().get('page', {}).get('buckets', []):
        for c in bucket.get('contents', []):
            items.append(c)
    return items


def parse_catalog_item(item):
    """
    Normalize a catalog item.
    Returns dict with: base_name, variant, is_spanish, date, watch_url — or None
    if it's not a real event replay (weigh-ins, docs, etc.).
    """
    raw = item.get('name', '') or ''
    name = raw

    is_spanish = 'en español' in name.lower()
    name = re.sub(r'\bEn Español\b\s*-?\s*', '', name, flags=re.IGNORECASE).strip()

    # Strip date prefix like "Thu, 4/16 - " / "Sat, 4/11 - "
    name = re.sub(r'^\w{3},\s*\d+/\d+\s*-\s*', '', name).strip()

    # Variant in trailing parens: (Main Card) / (Prelims)
    variant = None
    vm = re.search(r'\(([^)]+)\)\s*$', name)
    if vm:
        variant = vm.group(1).strip()
        name = re.sub(r'\s*\([^)]+\)\s*$', '', name).strip()

    # Skip non-event content
    if re.search(r'weigh-?in|story|presents|\bEp\.?\s*\d', raw, re.IGNORECASE):
        return None
    if not name.lower().startswith('pfl'):
        return None

    cid = item.get('id')
    if not cid:
        return None

    # The catalog item is a *listing* wrapper; the playable replay lives in
    # streams[]. The ESPN watch player needs the STREAM id, which often differs
    # from the listing id (e.g. an English Main-Card listing whose on-demand
    # replay stream carries a different guid) — using the listing id yields a
    # link that doesn't resolve. Among primary streams, prefer the on-demand
    # 'online' replay over the 'linear' live-channel simulcast (the linear id
    # points at the live feed, not the VOD); break ties by longest duration.
    # Fall back to the listing id if the item carries no streams.
    streams = item.get('streams') or []
    primary = [s for s in streams if s.get('tier') == 'primary'] or streams
    def _stream_rank(s):
        is_linear = (s.get('source') or {}).get('type') == 'linear'
        return (1 if is_linear else 0, -(s.get('durationInSeconds') or 0))
    stream = min(primary, key=_stream_rank) if primary else None
    watch_id = (stream or {}).get('id') or cid

    espn_date = None
    utc = item.get('utc')
    if utc:
        try:
            espn_date = datetime.fromisoformat(utc).date()
        except ValueError:
            pass

    return {
        'base_name':  name,
        'variant':    variant,
        'is_spanish': is_spanish,
        'date':       espn_date,
        'watch_url':  WATCH_URL_TMPL.format(watch_id),
    }


def variant_slot(variant):
    """Bucket a catalog variant into 'main' or 'prelims'."""
    return 'prelims' if 'prelim' in (variant or '').lower() else 'main'

# ── Matching ──────────────────────────────────────────────────────────────────

def parse_db_date(date_str):
    # Supabase returns ISO dates ('2026-07-25'); the '%B %d, %Y' forms are kept
    # for any legacy free-text dates. ISO must be first — without it this
    # returned None for every event, silently disabling all date logic below.
    for fmt in ('%Y-%m-%d', '%B %d, %Y', '%b %d, %Y'):
        try:
            return datetime.strptime(date_str, fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def main(push=False):
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise ValueError('Missing SUPABASE_URL or SUPABASE_KEY in .env file')

    print('Fetching PFL events from Supabase...')
    db_events = fetch_pfl_events()
    print(f'  {len(db_events)} PFL events')

    print('Fetching ESPN PFL watch catalog...')
    raw_items = fetch_catalog()
    items = [p for p in (parse_catalog_item(i) for i in raw_items) if p]
    print(f'  {len(items)} event replays in catalog\n')

    # Per ESPN event collect best main-card + best prelims replay.
    # English is preferred over the Spanish broadcast for each slot.
    espn_events = {}  # base_name.lower() -> {base_name, date, main, prelims}
    for it in items:
        key = it['base_name'].lower()
        entry = espn_events.setdefault(
            key, {'base_name': it['base_name'], 'date': it['date'],
                  'main': None, 'prelims': None})
        if it['date'] and not entry['date']:
            entry['date'] = it['date']
        slot = variant_slot(it['variant'])
        cur = entry[slot]
        if cur is None or (cur['is_spanish'] and not it['is_spanish']):
            entry[slot] = it

    matched, unmatched = [], []

    for entry in espn_events.values():
        base = entry['base_name'].lower()
        # DB event name starts with the ESPN base name ("PFL Chicago" ⊂
        # "PFL Chicago: Pettis vs. McKee").
        candidates = [e for e in db_events if e['name'].lower().startswith(base)]

        # Fallback for city-name form mismatches: ESPN abbreviates some cities
        # differently than Sherdog (ESPN "PFL D.C." vs Sherdog "PFL Washington
        # D.C.: Jean vs. Rodriguez"), so the name prefix doesn't line up. PFL
        # runs at most one card per day, so match by air date instead — a PFL
        # event within a day of the ESPN date is the same event. If this yields
        # more than one it falls through to 'ambiguous' below (never mis-stored).
        if not candidates and entry['date']:
            candidates = [e for e in db_events
                          if parse_db_date(e['date'])
                          and abs((parse_db_date(e['date']) - entry['date']).days) <= 1]

        # Narrow by date when there's more than one
        if len(candidates) > 1 and entry['date']:
            dated = [e for e in candidates
                     if (parse_db_date(e['date']) and
                         abs((parse_db_date(e['date']) - entry['date']).days) <= 2)]
            if dated:
                candidates = dated

        if len(candidates) == 1:
            matched.append((candidates[0], entry))
        elif not candidates:
            unmatched.append((entry, 'no DB event'))
        else:
            unmatched.append((entry, f'{len(candidates)} ambiguous'))

    # ── Write review file ─────────────────────────────────────────────────────
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as out:
        out.write('ESPN PFL VIDEO MATCH RESULTS\n')
        out.write(f'Generated: {datetime.now():%Y-%m-%d %H:%M}\n')
        out.write(f'Matched: {len(matched)}  |  Unmatched: {len(unmatched)}\n')
        out.write('=' * 70 + '\n\n')
        out.write(f'MATCHED ({len(matched)})\n' + '-' * 70 + '\n')
        for ev, entry in matched:
            main_url    = entry['main']['watch_url']    if entry['main']    else '(none)'
            prelims_url = entry['prelims']['watch_url'] if entry['prelims'] else '(none)'
            out.write(f"event:    {ev['name']} ({ev['date']})\n")
            out.write(f"main:     {main_url}\n")
            out.write(f"prelims:  {prelims_url}\n\n")
        out.write(f'\nUNMATCHED ({len(unmatched)})\n' + '-' * 70 + '\n')
        for entry, reason in unmatched:
            out.write(f"{reason:<16} | {entry['base_name']} | {entry['date']}\n")

    print(f'Matched: {len(matched)}  |  Unmatched: {len(unmatched)}')
    print(f'Saved to: {OUTPUT_FILE}\n')

    if push:
        if matched:
            print(f'Pushing ESPN URLs for {len(matched)} events to Supabase...')
            ok = 0
            for ev, entry in matched:
                main_url    = entry['main']['watch_url']    if entry['main']    else None
                prelims_url = entry['prelims']['watch_url'] if entry['prelims'] else None
                if update_espn_urls(ev['id'], main_url, prelims_url):
                    ok += 1
            print(f'  updated {ok}/{len(matched)} events')
    else:
        print('Dry run — pass --push to write events.espn_url / espn_prelims_url')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--push', action='store_true',
                        help='Write matched URLs to events.espn_url')
    args = parser.parse_args()
    main(push=args.push)
