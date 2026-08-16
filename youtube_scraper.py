#!/usr/bin/env python3
"""
youtube_scraper.py
Matches full-fight videos from official promotion YouTube channels to fights in
Supabase. Each channel in CHANNELS covers one or more DB orgs:
  - PFL channel   → PFL, Bellator, WSOF (PFL absorbed both); full-event uploads
  - RIZIN channel → RIZIN (per-fight only)
  - ONE channel   → ONE Championship (per-fight, MMA bouts only)
  - BRAVE channel → Brave CF (per-fight)
Stores the video URL in fights.youtube_url (per fight), events.youtube_url
(full main-card replays), and events.youtube_prelims_url (prelim-card
replays, e.g. LFA's 'Full Prelim' uploads)
(full-event videos). The full-event matcher is org-agnostic — any channel that
posts '... Full Event' uploads gets matched; today only PFL does on YouTube
(ONE/Brave post full events on their own paywalled platforms).

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
from datetime import date

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

# Each channel covers one or more DB organizations. Adding a new promotion's
# official YouTube channel = appending an entry here. Every channel gets both
# passes: per-fight ('full fight' playlists) and full-event (recent uploads
# whose title carries a 'full event' marker — org-agnostic, see
# match_full_event). Channels that only post per-fight simply yield no
# full-event matches; nothing to configure per channel.
CHANNELS = [
    {
        # PFL — posts full-event uploads as 'YYYY [<region>:] PFL <city> |
        # Full Event' (e.g. '2026 MENA: PFL Riyadh', '2026 PFL Africa: Nigeria').
        'name':       'PFL',
        'channel_id': 'UCPrONRG9hO1f-OrxW-WH-xg',
        'orgs':       ['PFL', 'Bellator', 'WSOF'],
    },
    {
        # RIZIN Fighting Federation. Title format on per-fight uploads is
        #   'Full Fight | <Japanese> vs. <Japanese> / Romanized A vs. Romanized B - YYYY/M/D RIZIN LANDMARK XX'
        # — needs '/' added as a segment delimiter in parse_fight_candidates.
        # Per-fight only; no full-event uploads.
        'name':       'RIZIN',
        'channel_id': 'UCZZ0UGjWsRdM8_5bsqtxYaQ',
        'orgs':       ['RIZIN'],
    },
    {
        # ONE Championship. Full fights live in a handful of event-named
        # playlists ('ONE: FISTS OF FURY Full Fights', ...). Per-fight titles:
        # '<flag> Superlek vs. <flag> Takeru | Full Fight' — flags/CJK are
        # stripped by the non-ASCII pass in parse_fight_candidates. Muay
        # Thai/kickboxing bouts don't resolve against the MMA fighter DB and
        # drop out as unmatched (expected — Sherdog indexes only ONE's MMA).
        # ONE DOES post themed full-event replays inside its 'Full Fights'
        # playlists (e.g. 'ONE Championship: FISTS OF FURY | Full Event') and the
        # matcher resolves all of them — but ONE geo-blocks them out of the US
        # (regionRestriction.blocked includes US), so with COUNTRY='US' they're
        # matched then dropped by the geo pass. Nothing stored for a US audience;
        # auto-stores if COUNTRY changes or ONE lifts the block.
        'name':       'ONE',
        'channel_id': 'UCiormkBf3jm6mfb7k0yPbKA',
        'orgs':       ['ONE Championship'],
    },
    {
        # BRAVE CF. Full fights collected in a 'FREE FULL FIGHTS' compilation
        # plus per-event 'Full Fight Coverage' playlists. Full events live on
        # paywalled BRAVE TV — YouTube only carries highlights/recaps, which the
        # full-event matcher rejects (see _NOT_FULL_EVENT).
        'name':       'Brave CF',
        'channel_id': 'UCos8j6dB44G_nlNpdJdP8SQ',
        'orgs':       ['Brave CF'],
    },
    {
        # LFA — posts BOTH card halves as full replays:
        #   'LFA 236 Main Card | Full Event - Five Exclusive Fights | LFA MMA'
        #   'LFA 236 Full Prelim | Exclusive Fights | LFA Fights'
        # The prelim titles carry no 'full event' marker — the variant logic
        # (_full_card_variant) routes them to events.youtube_prelims_url.
        # Busy channel (clips/interviews between replays) — scan deeper than
        # the default 400 uploads so older replays backfill.
        'name':       'LFA',
        'channel_id': 'UCMssVoZVgjD4pyeG6Ic-_Nw',
        'orgs':       ['LFA'],
        'max_pages':  40,
    },
]
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

def update_event_youtube_url(event_id, url, variant='main'):
    column = 'youtube_prelims_url' if variant == 'prelims' else 'youtube_url'
    r = requests.patch(f'{SUPABASE_URL}/rest/v1/events?id=eq.{event_id}',
                       headers=_sb_headers(),
                       data=json.dumps({column: url}))
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

def get_full_fight_playlists(channel_id):
    """All playlists for `channel_id` whose title mentions 'full fight'."""
    playlists = []
    for pl in yt_paginated('playlists', {
        'part': 'snippet', 'channelId': channel_id, 'maxResults': 50,
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

def get_full_event_videos(channel_id, max_pages=8):
    """
    Scan a channel's most-recent uploads for '... full event' videos.
    Returns (video_id, title) tuples.

    Bounded to `max_pages` × 50 = 400 recent uploads: new full events surface at
    the top of the feed and older ones were captured (and stored) by prior runs,
    so a bounded scan keeps this cheap on channels with huge back catalogues
    (ONE, Brave) without losing coverage on channels that do post them (PFL).
    """
    uploads_playlist = 'UU' + channel_id[2:]   # uploads playlist id
    out, page_token, pages = [], None, 0
    while pages < max_pages:
        data = yt_get('playlistItems', {
            'part': 'snippet', 'playlistId': uploads_playlist, 'maxResults': 50,
            **({'pageToken': page_token} if page_token else {}),
        })
        pages += 1
        for item in data.get('items', []):
            sn = item['snippet']
            title = sn.get('title', '')
            vid = sn.get('resourceId', {}).get('videoId')
            if vid and _is_full_event_title(title):
                out.append((vid, title))
        page_token = data.get('nextPageToken')
        if not page_token:
            break
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

    Handles (PFL/Bellator):
      "(HD FULL FIGHT) Cedric Doumbe vs Jordan Zebo | PFL Paris (...)"
      "Full Fight | Dakota Ditcheva vs Cornelia Holm | PFL Paris"
      "Clay Collard vs Shane Burgos (Lightweight Semifinal) | 2023 PFL Playoffs"
      "Title!! | Sergio Pettis vs Kyoji Horiguchi 堀口恭司 | Full Fight | Bellator 272"
    Plus (RIZIN-style):
      "Full Fight | 富沢ダイチ vs. 加藤瑠依 / Daichi Tomizawa vs. Rui Kato - 2026/6/6 RIZIN LANDMARK 14 in SENDAI"
        — needs '/' added as a segment delimiter, and the trailing
        ' - YYYY/M/D ...' stripped from fighter B.
    """
    candidates = []
    # Split on '|' AND '/' so RIZIN's romanized pair gets its own segment.
    for seg in re.split(r'[|/]', title):
        seg = re.sub(r'\([^)]*\)', '', seg)         # drop parentheticals
        seg = re.sub(r'[^\x00-\x7F]+', '', seg)     # drop non-ASCII (CJK names)
        seg = re.sub(r'\s+\d+\s*$', '', seg).strip()  # drop trailing rematch num
        # "A vs B" or Bellator-style "A v B"
        m = re.search(r'(.+?)\s+vs?\.?\s+(.+)$', seg, re.IGNORECASE)
        if not m:
            continue
        a = m.group(1).strip(' -–—!?.')
        b = m.group(2).strip(' -–—!?.')
        # Strip trailing ' - YYYY/M/D EVENT' / 'YYYY.M.D EVENT' / etc. from
        # fighter B — RIZIN per-fight titles use both 'YYYY/M/D' and 'YYYY.M.D'.
        b = re.sub(r'\s*[-–—]?\s*\d{4}[\./]\d+[\./]\d+.*$', '', b).strip()
        # Strip stray brackets that survive the date trim.
        b = b.strip('[](){}!? -–—.')
        if a and b and len(a) > 1 and len(b) > 1:
            candidates.append((a, b))
    return candidates

def playlist_event_hint(playlist_title):
    """'PFL Paris: Full Fight Videos' -> 'pfl paris'."""
    name = re.split(r'\s*:?\s*full fight', playlist_title, flags=re.IGNORECASE)[0]
    return name.strip().lower()

# ── Matching ──────────────────────────────────────────────────────────────────

def surname(name):
    """Last-name token for the fallback index, ignoring trailing generational
    suffixes (Jr., Sr., II–IV). Without this every '… Jr.' keys under 'jr.', so
    a clip like 'Michael Aswell Jr.' collides with every other Junior on lookup.
    Must be used for both index construction and lookup so the keys line up."""
    toks = name.split()
    while len(toks) > 1 and toks[-1].lower().rstrip('.') in {'jr', 'sr', 'ii', 'iii', 'iv'}:
        toks = toks[:-1]
    return toks[-1].lower()

def _is_future_event(ev):
    """True if the event is confirmed to be in the future. A full-fight video
    can't exist for a bout that hasn't happened, so such candidates are dropped.
    Undated/unparseable events return False (kept)."""
    d = (ev.get('date') or '')[:10]
    try:
        return date.fromisoformat(d) > date.today()
    except ValueError:
        return False

def build_lookups(fighters, aliases, channel_fights, events):
    fighter_by_name  = {}
    fighter_by_last  = {}
    fighter_by_alias = {}
    fighter_names    = {}
    for f in fighters:
        fid, name = f['id'], f['name']
        fighter_names[fid] = name
        fighter_by_name[name.lower()] = fid
        fighter_by_last.setdefault(surname(name), []).append(fid)
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
    return by_last.get(surname(nl), [])

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

    # Drop future-dated bouts: a full-fight video can't exist for a fight that
    # hasn't happened yet, so a historical fight re-surfaced ahead of a scheduled
    # rematch won't attach to the upcoming rematch's row.
    non_future = [c for c in candidates
                  if not _is_future_event(event_by_id.get(c['event_id'], {}))]
    if not non_future:
        return None, f'future event only: {name_a} vs {name_b}'
    candidates = non_future

    if len(candidates) == 1:
        return candidates[0], 'matched'

    # Multiple (rematch) → disambiguate by playlist event hint
    if event_hint:
        hits = [c for c in candidates
                if event_hint in event_by_id.get(c['event_id'], {}).get('name', '').lower()]
        if len(hits) == 1:
            return hits[0], 'matched (by event)'

    return None, f'ambiguous ({len(candidates)} fights): {name_a} vs {name_b}'

# Markers that identify a highlights / recap / promo clip rather than a genuine
# full-event upload. A title like 'BRAVE 100 ... | Full Event Highlights' carries
# 'full event' but is NOT one — reject it so it never attaches to an event.
_NOT_FULL_EVENT = re.compile(
    r'\b(highlights?|recap|vlog|preview|promo|trailer|teaser)\b', re.IGNORECASE)

# Roman numerals 2-10 → arabic. ONE files its themed cards as 'FISTS OF FURY II'
# on YouTube but 'Fists of Fury 2' on Sherdog; the numeral is often the only
# thing separating an event from its sibling, so it must survive tokenization.
# 'i' (=1) is deliberately omitted — a lone 'i' is too collision-prone and a
# base event has no number anyway ('Fists of Fury', not 'Fists of Fury 1').
_ROMAN = {'ii': '2', 'iii': '3', 'iv': '4', 'v': '5', 'vi': '6',
          'vii': '7', 'viii': '8', 'ix': '9', 'x': '10'}

def _is_full_event_title(title):
    """A video that is a full card replay — '... | Full Event' (main card) or
    '... Full Prelim(s)' (LFA's prelim-half uploads) — rather than a single
    bout. Both routes send such titles to match_full_event."""
    return bool(re.search(r'\bfull (?:event|card|prelims?)\b', title, re.IGNORECASE))

def _full_card_variant(title):
    """'prelims' for prelim-card replays, else 'main'. Routed to
    events.youtube_prelims_url / events.youtube_url respectively."""
    return 'prelims' if re.search(r'\bprelims?\b', title, re.IGNORECASE) else 'main'

def _sig_tokens(text):
    """Significant tokens: alpha words >2 chars + standalone numbers, with
    roman numerals (II–X) folded to arabic so 'FISTS OF FURY II' and
    'Fists of Fury 2' produce the same token set."""
    out = []
    for t in re.split(r'[^a-z0-9]+', text.lower()):
        t = _ROMAN.get(t, t)
        if (t.isalpha() and len(t) > 2) or t.isdigit():
            out.append(t)
    return out

def match_full_event(title, events):
    """
    Match a '... full event' video title to one of `events` (already scoped to
    the video's channel orgs). Org-agnostic — no promotion name is hard-coded.

    Strip the 'full event' marker, @collab handles and parentheticals, drop the
    year (if any), and tokenize (see _sig_tokens). The event's own organization
    name is treated as implied — its tokens ('one', 'championship', 'pfl') don't
    have to appear in the video title — because events are already org-scoped.
    Match tiers, strongest first:
      1. exact  — video's non-org tokens == event's non-org name tokens. Needed
                  to split themed siblings ('Fists of Fury' vs 'Fists of Fury 2';
                  a base card must not also match its numbered follow-ups).
      2. name   — every video token is in the event name.
      3. name+location — every video token is in name OR location.

    Handles both PFL's regional uploads and ONE's themed playlist full events:
      "2026 PFL San Diego | Full Event"                 -> PFL San Diego
      "2026 MENA: PFL Riyadh | Full Event"              -> PFL MENA (in Riyadh)
      "ONE Championship: FISTS OF FURY | Full Event"    -> Fists of Fury
      "ONE Championship: FISTS OF FURY II | Full Event" -> Fists of Fury 2
    Returns (event, reason).
    """
    if _NOT_FULL_EVENT.search(title):
        return None, 'not a full event (highlights/recap/promo)'

    # Two candidate keys, tried in order: (1) everything before the full-card
    # marker; (2) just the first pipe-segment — LFA slots hype filler between
    # the event name and the marker ('LFA 234 | TWO world title fight | Full
    # Card | ...'), and the first segment alone carries the identity.
    raw_keys = [re.split(r'\|?\s*full (?:event|card|prelims?)\b', title,
                         maxsplit=1, flags=re.IGNORECASE)[0]]
    first_seg = title.split('|')[0]
    if first_seg.strip() and first_seg not in raw_keys:
        raw_keys.append(first_seg)

    last_reason = 'no key tokens'
    for key in raw_keys:
        key = re.sub(r'@\S+', ' ', key)          # drop @collab handles
        key = re.sub(r'\([^)]*\)', ' ', key)     # drop parentheticals
        # 'Main Card' is card-half filler, not event identity ('LFA 236 Main
        # Card | Full Event ...') — never part of the Sherdog name.
        key = re.sub(r'\bmain\s+card\b', ' ', key, flags=re.IGNORECASE)
        ym = re.search(r'\b(?:19|20)\d{2}\b', key)
        year = ym.group(0) if ym else None
        if year:
            key = key.replace(year, ' ')
        vid = set(_sig_tokens(key))
        if not vid:
            continue

        exact, name_hits, hits = [], [], []
        for e in events:
            if year and year not in (e.get('date') or ''):
                continue
            org  = set(re.split(r'[^a-z0-9]+', (e.get('organization') or '').lower()))
            name = set(_sig_tokens(e['name']))
            loc  = set(re.split(r'[^a-z0-9]+', (e.get('location') or '').lower()))
            v = vid - org      # org name is implied by scope — don't require it
            if not v:
                continue
            if v == (name - org):
                exact.append(e)
            if v <= name:
                name_hits.append(e)
            if v <= (name | loc):
                hits.append(e)

        label = f'{year or "any"} / {"+".join(sorted(vid))}'
        if len(exact) == 1:
            return exact[0], 'matched (exact)'
        if len(name_hits) == 1:
            return name_hits[0], 'matched'
        if len(hits) == 1:
            return hits[0], 'matched'
        if exact:
            last_reason = f'ambiguous exact ({len(exact)}): {label}'
        elif not hits:
            last_reason = f'no event ({label})'
        else:
            last_reason = f'ambiguous ({len(hits)} events: {label})'
    return None, last_reason

# ── Main ──────────────────────────────────────────────────────────────────────

def main(push=False):
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise ValueError('Missing SUPABASE_URL or SUPABASE_KEY in .env')
    if not YOUTUBE_API_KEY:
        raise ValueError('Missing YOUTUBE_API_KEY in .env')

    print('Loading data from Supabase...')
    fighters = fetch_all('fighters', 'id,name')
    aliases  = fetch_all('fighter_aliases', 'alias,fighter_id')
    all_fights = fetch_all('fights', 'id,event_id,fighter1_id,fighter2_id')

    matched, unmatched = [], []
    ev_matched, ev_unmatched = [], []
    seen_fight = set()
    seen_event = set()

    def record_full_event(vid, vtitle, events):
        """Match a full-card video ('Full Event' main card or 'Full Prelim')
        to one of `events` and record it, deduped by (event, variant). Used
        from both the playlist scan (ONE files full events inside per-fight
        playlists) and the uploads scan (PFL, LFA)."""
        url = VIDEO_URL_TMPL.format(vid)
        event, reason = match_full_event(vtitle, events)
        if not event:
            ev_unmatched.append((reason, vtitle, url))
            return
        variant = _full_card_variant(vtitle)
        if (event['id'], variant) in seen_event:
            return
        seen_event.add((event['id'], variant))
        ev_matched.append({
            'event_id': event['id'], 'event_name': event['name'],
            'video_title': vtitle, 'url': url, 'video_id': vid,
            'variant': variant,
        })

    # Walk each channel independently — its DB scope is the events for its
    # orgs (e.g. PFL channel only matches PFL/Bellator/WSOF events).
    for ch in CHANNELS:
        org_filter = ','.join(ch['orgs'])
        events = fetch_all('events', 'id,name,date,location,organization',
                           f'&organization=in.({org_filter})')
        channel_event_ids = {e['id'] for e in events}
        channel_fights = [f for f in all_fights if f['event_id'] in channel_event_ids]
        print(f'\n=== Channel: {ch["name"]} (orgs={ch["orgs"]}) ===')
        print(f'  {len(events)} events, {len(channel_fights)} fights in scope')

        (by_name, by_last, by_alias,
         fighter_names, event_by_id, fight_lookup) = build_lookups(
            fighters, aliases, channel_fights, events)
        lookups = (by_name, by_last, by_alias, fighter_names, event_by_id, fight_lookup)

        playlists = get_full_fight_playlists(ch['channel_id'])
        print(f'  {len(playlists)} full-fight playlists')

        for pl in playlists:
            hint = playlist_event_hint(pl['title'])
            videos = get_playlist_videos(pl['id'])
            print(f'    [{pl["title"]}] — {len(videos)} videos')
            for vid, vtitle in videos:
                url = VIDEO_URL_TMPL.format(vid)
                # A playlist can hold event replays alongside single bouts
                # (ONE's themed 'Full Fights' playlists include a '... | Full
                # Event' video). Route those to the full-event matcher, not the
                # A-vs-B parser (which would just PARSE_FAIL on them).
                if _is_full_event_title(vtitle):
                    record_full_event(vid, vtitle, events)
                    continue
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

        # Full-event pass — runs for every channel; org-agnostic matcher, so
        # channels that don't post full events simply yield nothing.
        print(f'  Scanning {ch["name"]} recent uploads for full-card videos...')
        fe_videos = get_full_event_videos(ch['channel_id'],
                                          max_pages=ch.get('max_pages', 8))
        print(f'    {len(fe_videos)} full-event candidate videos found')
        for vid, vtitle in fe_videos:
            record_full_event(vid, vtitle, events)

    # 'events' is per-channel above; for the geo-block + write-file passes
    # below we use a unified event_by_id keyed off matched results.
    events = fetch_all('events', 'id,name,date,location')

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
        out.write('YOUTUBE MATCH RESULTS\n')
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
            out.write(f"event_id: {m['event_id']}  [{m.get('variant', 'main')}]\n")
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
            ok = sum(update_event_youtube_url(m['event_id'], m['url'],
                                              m.get('variant', 'main'))
                     for m in ev_matched)
            print(f'  updated {ok}/{len(ev_matched)} events')
    else:
        print('\nDry run — pass --push to write youtube_url columns')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--push', action='store_true',
                        help='Write matched URLs to fights.youtube_url')
    args = parser.parse_args()
    main(push=args.push)
