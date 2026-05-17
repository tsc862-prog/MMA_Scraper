#!/usr/bin/env python3
"""
youtube_scraper.py
Matches full-fight videos from the official PFL YouTube channel to fights in
Supabase. The PFL channel covers PFL, Bellator and WSOF (PFL absorbed both).
Stores the video URL in fights.youtube_url (per fight) and events.youtube_url
(full-event videos).

Uses the YouTube Data API v3 — set YOUTUBE_API_KEY in .env.

  python youtube_scraper.py            # dry run, writes txt only
  python youtube_scraper.py --push     # also update youtube_url columns
"""

import os
import re
import sys
import json
import argparse
import requests

# Load .env
_env_path = os.path.join(os.path.dirname(__file__), '.env')
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith('#') and '=' in _line:
                _k, _v = _line.split('=', 1)
                os.environ.setdefault(_k.strip(), _v.strip())

SUPABASE_URL    = os.environ.get('SUPABASE_URL', '').rstrip('/')
SUPABASE_KEY    = os.environ.get('SUPABASE_KEY', '')
YOUTUBE_API_KEY = os.environ.get('YOUTUBE_API_KEY', '')

PFL_CHANNEL_ID = 'UCPrONRG9hO1f-OrxW-WH-xg'
YT_API         = 'https://www.googleapis.com/youtube/v3'
VIDEO_URL_TMPL = 'https://www.youtube.com/watch?v={}'
OUTPUT_FILE    = 'youtube_matches.txt'
COUNTRY        = 'US'   # only store videos viewable from this country

sys.stdout.reconfigure(encoding='utf-8')

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
        url = (f'{SUPABASE_URL}/rest/v1/{table}'
               f'?select={select}&limit={PAGE}&offset={offset}{extra}')
        r = requests.get(url, headers=_sb_headers())
        if not r.ok:
            print(f'  [WARN] fetch {table} failed ({r.status_code}): {r.text[:200]}')
            break
        batch = r.json()
        out.extend(batch)
        if len(batch) < PAGE:
            break
        offset += PAGE
    return out

def update_youtube_url(fight_id, url):
    r = requests.patch(
        f'{SUPABASE_URL}/rest/v1/fights?id=eq.{fight_id}',
        headers=_sb_headers(),
        data=json.dumps({'youtube_url': url}),
    )
    return r.status_code in (200, 204)

def update_event_youtube_url(event_id, url):
    r = requests.patch(
        f'{SUPABASE_URL}/rest/v1/events?id=eq.{event_id}',
        headers=_sb_headers(),
        data=json.dumps({'youtube_url': url}),
    )
    return r.status_code in (200, 204)

# ── YouTube Data API ──────────────────────────────────────────────────────────

def yt_get(endpoint, params):
    """Single YouTube Data API call."""
    params = {**params, 'key': YOUTUBE_API_KEY}
    r = requests.get(f'{YT_API}/{endpoint}', params=params, timeout=30)
    if not r.ok:
        raise RuntimeError(f'YouTube API {endpoint} failed '
                           f'({r.status_code}): {r.text[:300]}')
    return r.json()

def yt_paginated(endpoint, params):
    """Yield all items across pages."""
    page_token = None
    while True:
        data = yt_get(endpoint, {**params, **({'pageToken': page_token} if page_token else {})})
        yield from data.get('items', [])
        page_token = data.get('nextPageToken')
        if not page_token:
            break

def get_full_fight_playlists():
    """All channel playlists whose title mentions 'full fight'."""
    playlists = []
    for pl in yt_paginated('playlists', {
        'part': 'snippet', 'channelId': PFL_CHANNEL_ID, 'maxResults': 50,
    }):
        title = pl['snippet']['title']
        if 'full fight' in title.lower():
            playlists.append({'id': pl['id'], 'title': title})
    return playlists

def get_playlist_videos(playlist_id):
    """All videos in a playlist as (video_id, title) tuples."""
    videos = []
    for item in yt_paginated('playlistItems', {
        'part': 'snippet', 'playlistId': playlist_id, 'maxResults': 50,
    }):
        sn = item['snippet']
        vid = sn.get('resourceId', {}).get('videoId')
        if vid and sn.get('title') not in ('Deleted video', 'Private video'):
            videos.append((vid, sn['title']))
    return videos

def get_full_event_videos():
    """
    Scan the channel uploads for '... | Full Event' videos.
    Returns (video_id, title) tuples.
    """
    uploads_playlist = 'UU' + PFL_CHANNEL_ID[2:]   # uploads playlist id
    out = []
    for item in yt_paginated('playlistItems', {
        'part': 'snippet', 'playlistId': uploads_playlist, 'maxResults': 50,
    }):
        sn = item['snippet']
        title = sn.get('title', '')
        vid = sn.get('resourceId', {}).get('videoId')
        if vid and re.search(r'\bfull event\b', title, re.IGNORECASE):
            out.append((vid, title))
    return out

def _viewable(region_restriction, country):
    """True if a video is viewable from `country` given its regionRestriction."""
    if not region_restriction:
        return True
    blocked = region_restriction.get('blocked', [])
    allowed = region_restriction.get('allowed', [])
    if blocked and country in blocked:
        return False
    if allowed and country not in allowed:
        return False
    return True

def get_blocked_video_ids(video_ids, country=COUNTRY):
    """Return the set of video IDs that are NOT viewable from `country`."""
    blocked = set()
    ids = list(dict.fromkeys(video_ids))   # dedupe, preserve order
    for i in range(0, len(ids), 50):       # videos.list accepts 50 ids/call
        batch = ids[i:i + 50]
        data = yt_get('videos', {'part': 'contentDetails', 'id': ','.join(batch)})
        for item in data.get('items', []):
            rr = item.get('contentDetails', {}).get('regionRestriction')
            if not _viewable(rr, country):
                blocked.add(item['id'])
    return blocked

# ── Title parsing ─────────────────────────────────────────────────────────────

def parse_fight_candidates(title):
    """
    Return a list of plausible (fighter_a, fighter_b) pairs from a YouTube
    full-fight title. The real matchup is whichever pair resolves against the
    fighter DB — titles put the event tag in many forms, so every
    pipe-delimited segment is tried.

    Handles:
      "(HD FULL FIGHT) Cedric Doumbe vs Jordan Zebo | PFL Paris (...)"
      "Full Fight | Dakota Ditcheva vs Cornelia Holm | PFL Paris"
      "Clay Collard vs Shane Burgos (Lightweight Semifinal) | 2023 PFL Playoffs"
      "Title!! | Sergio Pettis vs Kyoji Horiguchi 堀口恭司 | Full Fight | Bellator 272"
    """
    candidates = []
    for seg in title.split('|'):
        seg = re.sub(r'\([^)]*\)', '', seg)         # drop parentheticals
        seg = re.sub(r'[^\x00-\x7F]+', '', seg)     # drop non-ASCII (CJK names)
        seg = re.sub(r'\s+\d+\s*$', '', seg).strip()  # drop trailing rematch num
        # "A vs B" or Bellator-style "A v B"
        m = re.search(r'(.+?)\s+vs?\.?\s+(.+)$', seg, re.IGNORECASE)
        if not m:
            continue
        a = m.group(1).strip(' -–—!?.')
        b = m.group(2).strip(' -–—!?.')
        if a and b and len(a) > 1 and len(b) > 1:
            candidates.append((a, b))
    return candidates

def playlist_event_hint(playlist_title):
    """'PFL Paris: Full Fight Videos' -> 'pfl paris'."""
    name = re.split(r'\s*:?\s*full fight', playlist_title, flags=re.IGNORECASE)[0]
    return name.strip().lower()

# ── Matching ──────────────────────────────────────────────────────────────────

def build_lookups(fighters, aliases, channel_fights, events):
    fighter_by_name  = {}
    fighter_by_last  = {}
    fighter_by_alias = {}
    fighter_names    = {}
    for f in fighters:
        fid, name = f['id'], f['name']
        fighter_names[fid] = name
        fighter_by_name[name.lower()] = fid
        fighter_by_last.setdefault(name.split()[-1].lower(), []).append(fid)
    for a in aliases:
        fighter_by_alias[a['alias'].lower()] = a['fighter_id']

    event_by_id = {e['id']: e for e in events}

    fight_lookup = {}
    for f in channel_fights:
        if f['fighter1_id'] and f['fighter2_id']:
            key = frozenset((f['fighter1_id'], f['fighter2_id']))
            fight_lookup.setdefault(key, []).append(f)

    return (fighter_by_name, fighter_by_last, fighter_by_alias,
            fighter_names, event_by_id, fight_lookup)

def resolve_fighter(name, by_name, by_alias, by_last):
    nl = name.lower().strip()
    fid = by_name.get(nl)
    if fid:
        return [fid]
    fid = by_alias.get(nl)
    if fid:
        return [fid]
    return by_last.get(nl.split()[-1], [])

def find_fight(name_a, name_b, event_hint, lookups):
    (by_name, by_last, by_alias, fighter_names, event_by_id, fight_lookup) = lookups

    ids_a = resolve_fighter(name_a, by_name, by_alias, by_last)
    ids_b = resolve_fighter(name_b, by_name, by_alias, by_last)
    if not ids_a:
        return None, f'fighter not found: "{name_a}"'
    if not ids_b:
        return None, f'fighter not found: "{name_b}"'

    candidates = []
    for a in ids_a:
        for b in ids_b:
            candidates.extend(fight_lookup.get(frozenset((a, b)), []))
    if not candidates:
        return None, f'no fight for {name_a} vs {name_b}'

    if len(candidates) == 1:
        return candidates[0], 'matched'

    # Multiple (rematch) → disambiguate by playlist event hint
    if event_hint:
        hits = [c for c in candidates
                if event_hint in event_by_id.get(c['event_id'], {}).get('name', '').lower()]
        if len(hits) == 1:
            return hits[0], 'matched (by event)'

    return None, f'ambiguous ({len(candidates)} fights): {name_a} vs {name_b}'

def match_full_event(title, events):
    """
    Match a '... | Full Event' video title to a PFL event.
    Handles the consistent recent format "[YEAR] PFL <KEY> | Full Event",
    matching KEY tokens against the event name + location.
    Returns (event, reason).
    """
    m = re.match(r'^\s*(\d{4})\s+PFL\s+(.+?)\s*\|\s*full event', title, re.IGNORECASE)
    if not m:
        return None, 'unrecognized title format'
    year, key = m.group(1), m.group(2)
    tokens = [t for t in re.split(r'[^a-z0-9]+', key.lower()) if len(t) > 2]
    if not tokens:
        return None, 'no key tokens'

    hits = []          # token matched in name OR location
    name_hits = []     # token matched in event name alone
    for e in events:
        if year not in (e.get('date') or ''):
            continue
        name_words = set(re.split(r'[^a-z0-9]+', e['name'].lower()))
        loc_words  = set(re.split(r'[^a-z0-9]+', (e.get('location') or '').lower()))
        full = name_words | loc_words
        if all(tok in full for tok in tokens):
            hits.append(e)
        if all(tok in name_words for tok in tokens):
            name_hits.append(e)

    # A name match is stronger than a location-only match.
    if len(name_hits) == 1:
        return name_hits[0], 'matched'
    if len(hits) == 1:
        return hits[0], 'matched'
    if not hits:
        return None, f'no event ({year} / {"+".join(tokens)})'
    return None, f'ambiguous ({len(hits)} events: {year} / {"+".join(tokens)})'

# ── Main ──────────────────────────────────────────────────────────────────────

def main(push=False):
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise ValueError('Missing SUPABASE_URL or SUPABASE_KEY in .env')
    if not YOUTUBE_API_KEY:
        raise ValueError('Missing YOUTUBE_API_KEY in .env')

    # The official PFL YouTube channel posts full fights for PFL plus the
    # promotions PFL absorbed — Bellator and WSOF.
    print('Loading data from Supabase...')
    fighters = fetch_all('fighters', 'id,name')
    aliases  = fetch_all('fighter_aliases', 'alias,fighter_id')
    events   = fetch_all('events', 'id,name,date,location',
                         '&organization=in.(PFL,Bellator,WSOF)')
    channel_event_ids = {e['id'] for e in events}
    all_fights = fetch_all('fights', 'id,event_id,fighter1_id,fighter2_id')
    channel_fights = [f for f in all_fights if f['event_id'] in channel_event_ids]
    print(f'  {len(fighters)} fighters, {len(events)} events, '
          f'{len(channel_fights)} fights (PFL+Bellator+WSOF)')

    (by_name, by_last, by_alias,
     fighter_names, event_by_id, fight_lookup) = build_lookups(
        fighters, aliases, channel_fights, events)
    lookups = (by_name, by_last, by_alias, fighter_names, event_by_id, fight_lookup)

    print('Fetching PFL "Full Fight" playlists from YouTube...')
    playlists = get_full_fight_playlists()
    print(f'  {len(playlists)} full-fight playlists\n')

    matched, unmatched = [], []
    seen_fight = set()

    for pl in playlists:
        hint = playlist_event_hint(pl['title'])
        videos = get_playlist_videos(pl['id'])
        print(f'  [{pl["title"]}] — {len(videos)} videos')
        for vid, vtitle in videos:
            url = VIDEO_URL_TMPL.format(vid)
            cand_pairs = parse_fight_candidates(vtitle)
            if not cand_pairs:
                unmatched.append(('PARSE_FAIL', vtitle, url))
                continue

            fight = None
            reason = 'no candidate matched'
            for name_a, name_b in cand_pairs:
                fight, reason = find_fight(name_a, name_b, hint, lookups)
                if fight:
                    break

            if fight:
                if fight['id'] in seen_fight:
                    continue
                seen_fight.add(fight['id'])
                n1 = fighter_names.get(fight['fighter1_id'], '?')
                n2 = fighter_names.get(fight['fighter2_id'], '?')
                ev = event_by_id.get(fight['event_id'], {}).get('name', '?')
                matched.append({
                    'fight_id': fight['id'], 'label': f'{n1} vs {n2} @ {ev}',
                    'video_title': vtitle, 'url': url, 'video_id': vid,
                })
            else:
                unmatched.append((reason, vtitle, url))

    # ── Full-event pass — match "... | Full Event" uploads to events ──────────
    print('\nScanning channel uploads for "Full Event" videos...')
    fe_videos = get_full_event_videos()
    print(f'  {len(fe_videos)} full-event videos found')

    ev_matched, ev_unmatched = [], []
    seen_event = set()
    for vid, vtitle in fe_videos:
        url = VIDEO_URL_TMPL.format(vid)
        event, reason = match_full_event(vtitle, events)
        if event:
            if event['id'] in seen_event:
                continue
            seen_event.add(event['id'])
            ev_matched.append({
                'event_id': event['id'], 'event_name': event['name'],
                'video_title': vtitle, 'url': url, 'video_id': vid,
            })
        else:
            ev_unmatched.append((reason, vtitle, url))

    # ── Drop videos geo-blocked in COUNTRY ────────────────────────────────────
    print(f'\nChecking region restrictions ({COUNTRY})...')
    blocked_ids = get_blocked_video_ids(
        [m['video_id'] for m in matched] + [m['video_id'] for m in ev_matched])
    geo_fights = [m for m in matched    if m['video_id'] in blocked_ids]
    geo_events = [m for m in ev_matched if m['video_id'] in blocked_ids]
    matched    = [m for m in matched    if m['video_id'] not in blocked_ids]
    ev_matched = [m for m in ev_matched if m['video_id'] not in blocked_ids]
    print(f'  {len(geo_fights)} fight + {len(geo_events)} event videos '
          f'blocked in {COUNTRY} — not stored')

    # ── Write review file ─────────────────────────────────────────────────────
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as out:
        out.write('YOUTUBE PFL MATCH RESULTS\n')
        out.write(f'Fights  — matched: {len(matched)}  unmatched: {len(unmatched)}\n')
        out.write(f'Events  — matched: {len(ev_matched)}  unmatched: {len(ev_unmatched)}\n')
        out.write(f'Geo-blocked in {COUNTRY} (not stored): '
                  f'{len(geo_fights)} fights, {len(geo_events)} events\n')
        out.write('=' * 70 + '\n\n')

        out.write(f'MATCHED FIGHTS ({len(matched)})\n' + '-' * 70 + '\n')
        for m in matched:
            out.write(f"fight_id: {m['fight_id']}\n")
            out.write(f"fight:    {m['label']}\n")
            out.write(f"video:    {m['video_title']}\n")
            out.write(f"url:      {m['url']}\n\n")

        out.write(f'\nMATCHED FULL EVENTS ({len(ev_matched)})\n' + '-' * 70 + '\n')
        for m in ev_matched:
            out.write(f"event_id: {m['event_id']}\n")
            out.write(f"event:    {m['event_name']}\n")
            out.write(f"video:    {m['video_title']}\n")
            out.write(f"url:      {m['url']}\n\n")

        out.write(f'\nUNMATCHED FIGHTS ({len(unmatched)})\n' + '-' * 70 + '\n')
        for reason, vtitle, url in unmatched:
            out.write(f'{reason:<30} | {vtitle} | {url}\n')

        out.write(f'\nUNMATCHED FULL EVENTS ({len(ev_unmatched)})\n' + '-' * 70 + '\n')
        for reason, vtitle, url in ev_unmatched:
            out.write(f'{reason:<34} | {vtitle} | {url}\n')

        out.write(f'\nGEO-BLOCKED IN {COUNTRY} — NOT STORED '
                  f'({len(geo_fights) + len(geo_events)})\n' + '-' * 70 + '\n')
        for m in geo_fights:
            out.write(f"fight | {m['label']} | {m['url']}\n")
        for m in geo_events:
            out.write(f"event | {m['event_name']} | {m['url']}\n")

    print(f'\nFights — matched: {len(matched)}  unmatched: {len(unmatched)}')
    print(f'Events — matched: {len(ev_matched)}  unmatched: {len(ev_unmatched)}')
    print(f'Saved to: {OUTPUT_FILE}')

    if push:
        if matched:
            print(f'\nPushing {len(matched)} fight URLs to Supabase...')
            ok = sum(update_youtube_url(m['fight_id'], m['url']) for m in matched)
            print(f'  updated {ok}/{len(matched)} fights')
        if ev_matched:
            print(f'Pushing {len(ev_matched)} event URLs to Supabase...')
            ok = sum(update_event_youtube_url(m['event_id'], m['url']) for m in ev_matched)
            print(f'  updated {ok}/{len(ev_matched)} events')
    else:
        print('\nDry run — pass --push to write youtube_url columns')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--push', action='store_true',
                        help='Write matched URLs to fights.youtube_url')
    args = parser.parse_args()
    main(push=args.push)
