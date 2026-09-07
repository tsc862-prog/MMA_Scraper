#!/usr/bin/env python3
"""
youtube_scraper.py
Matches full-fight videos from official promotion YouTube channels to fights in
Supabase. Each channel in CHANNELS covers one or more DB orgs:
  - PFL channel      → PFL, Bellator, WSOF (PFL absorbed both); full-event uploads
  - RIZIN channel    → RIZIN (per-fight only)
  - ONE channel      → ONE Championship (per-fight, MMA bouts only)
  - BRAVE channel    → Brave CF (per-fight)
  - LFA channel      → LFA + Legacy FC (per-fight + main-card / prelim full
                       replays; most content sits in playlists that aren't
                       titled 'full fight', so the entry names them)
  - Bellator channel → Bellator, PFL, RIZIN cross-cards (per-fight archive of
                       ~1000 videos + 'Re-Air' full cards)
Stores the video URL in fights.youtube_url (per fight), events.youtube_url
(full main-card replays), and events.youtube_prelims_url (prelim-card
replays, e.g. LFA's 'Full Prelim' uploads). The full-event matcher is
org-agnostic — any channel that posts '... Full Event' / 'Re-Air' uploads
gets matched (ONE/Brave post full events on their own paywalled platforms).

Per-fight matching: fighter names are resolved exact → alias → surname; when
an exact-name hit yields no bout the surname pool is widened (namesakes).
Rematches are split by the event hint — the playlist title, or the video
title's own trailing event tag ('… | Bellator 238') — and failing that by the
series ordinal in the title ('Carvalho v Manhoef II' → 2nd meeting by date).

Uses the YouTube Data API v3 — set YOUTUBE_API_KEY in .env.

  python youtube_scraper.py                     # dry run, writes txt only
  python youtube_scraper.py --push              # also update youtube_url columns
  python youtube_scraper.py --channel Bellator  # scan one CHANNELS entry only
"""

import os
import re
import sys
import json
import argparse
import unicodedata
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
        # LFA (@lfafights) — posts BOTH card halves as full replays:
        #   'LFA 236 Main Card | Full Event - Five Exclusive Fights | LFA MMA'
        #   'LFA 236 Full Prelim | Exclusive Fights | LFA Fights'
        #   'LFA 241 Prelims Card *live* | 3 x Live Prelims | LFA Fights'
        # The prelim titles carry no 'full event' marker — the variant logic
        # (_full_card_variant) routes them to events.youtube_prelims_url.
        # Only one of its 19 playlists is titled 'Full Fights'; the rest of
        # the full-fight archive is filed under 'BEFORE THE UFC', 'WOMEN OF
        # MMA', 'Best of LFA' etc., and the full cards under 'LFA FULL EVENTS',
        # 'LFA Main Cards 2026' and the prelim-replay list — so they're named
        # here explicitly. Card playlists mix in single-bout clips, hence the
        # duration gate (MIN_FULL_CARD_SECS). The channel also re-airs Legacy
        # FC cards (LFA's predecessor; RFA, the other half, isn't in the DB).
        # Busy channel (clips/interviews between replays) — scan deeper than
        # the default 400 uploads so older replays backfill.
        'name':       'LFA',
        'channel_id': 'UCMssVoZVgjD4pyeG6Ic-_Nw',
        'orgs':       ['LFA', 'Legacy FC'],
        'max_pages':  40,
        'fight_playlists': [
            'PL6_GVS5dt2AUgdgMsaJuP28ATHqip8CPM',   # BEFORE THE UFC | LFA FIGHTERS
            'PL6_GVS5dt2AWhHMOFS2ls6jNP0oF5OEsZ',   # WOMEN OF MMA | LFA MMA
            'PL6_GVS5dt2AVXlzavNUaCOBesOsXIwUGP',   # Best of LFA
            'PL6_GVS5dt2AVvx4KursrXYLdO_uIXBkeW',   # LFA Classics
            'PL6_GVS5dt2AW3QjmYgwv3jQZd9KTrMKr6',   # Best fights
        ],
        # id → card half to assume when the title names neither ('main' /
        # 'prelims'); a title that does say 'Prelims' / 'Main Card' wins.
        'card_playlists': {
            'PL6_GVS5dt2AWUNLYLf_-KbYi7O3iS6MDR': 'main',      # LFA FULL EVENTS | Full Cards
            'PL6_GVS5dt2AXKqlsB2Jvn-csz4QmbIeL_': 'main',      # LFA Main Cards 2026 | Full MMA event
            'PL6_GVS5dt2AU-XD8KVqVnhhyes5pOz9WM': 'prelims',   # LFA Fight | *LIVE* LFA Prelim Replays
        },
    },
    {
        # Bellator's own channel (kept alive by PFL after the 2023 buyout, so
        # it also posts PFL bouts). One giant 'Full Fights | Bellator MMA'
        # playlist (~1000 videos, ~150 private) plus per-card 'Bellator 300 |
        # Full Fights' playlists. Titles are hype-first:
        #   'CYBORG ARRIVES! | Cris Cyborg v Julia Budd | Full Fight | Bellator 238'
        # — the A-v-B segment is found by the segment walk; the trailing
        # event tag becomes the per-video hint (title_event_hint) so rematches
        # ('Carvalho v Manhoef II') resolve. 'Bellator Kickboxing | Full
        # Fights' is scanned too but its kickboxers aren't in the MMA DB, so
        # they drop out as unmatched. Cross-promotion cards (Bellator vs
        # Rizin) live under RIZIN in the DB. Added 2026-09-06.
        'name':       'Bellator',
        'channel_id': 'UCnkMTsKYqhHm6l6GQzg4szg',
        'orgs':       ['Bellator', 'PFL', 'RIZIN'],
    },
]
YT_API         = 'https://www.googleapis.com/youtube/v3'
VIDEO_URL_TMPL = 'https://www.youtube.com/watch?v={}'
OUTPUT_FILE    = 'youtube_matches.txt'
COUNTRY        = 'US'   # only store videos viewable from this country
# A full-card replay runs hours; a single bout rarely passes 40 min even with
# walkouts. Full-card candidates shorter than this are single-fight clips or
# highlight reels that slipped past the title markers.
MIN_FULL_CARD_SECS = 40 * 60

sys.stdout.reconfigure(encoding='utf-8')

# ── Supabase helpers ──────────────────────────────────────────────────────────

def _sb_headers():
    return {
        'apikey': SUPABASE_KEY,
        'Authorization': f'Bearer {SUPABASE_KEY}',
        'Content-Type': 'application/json',
        'Prefer': 'return=minimal',
    }

def fetch_all(table, select='*', extra='', order='id'):
    # order=id: PostgREST offset paging without an ORDER BY is not stable —
    # rows can be skipped or repeated between pages (fighters is ~39 pages),
    # which surfaced as spurious 'fighter not found' misses.
    PAGE, offset, out = 1000, 0, []
    while True:
        url = (f'{SUPABASE_URL}/rest/v1/{table}'
               f'?select={select}&order={order}&limit={PAGE}&offset={offset}{extra}')
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

def get_full_fight_playlists(channel_id, extra_ids=()):
    """All playlists for `channel_id` whose title mentions 'full fight', plus
    any whose id is in `extra_ids` (a channel's hand-picked per-fight lists
    that aren't titled that way — LFA's 'BEFORE THE UFC', 'WOMEN OF MMA')."""
    playlists = []
    extra = set(extra_ids)
    for pl in yt_paginated('playlists', {
        'part': 'snippet', 'channelId': channel_id, 'maxResults': 50,
    }):
        title = pl['snippet']['title']
        if 'full fight' in title.lower() or pl['id'] in extra:
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

def get_video_details(video_ids, country=COUNTRY):
    """(blocked, durations): the set of video IDs NOT viewable from `country`,
    and {video_id: seconds} for every id with a known, non-zero duration.
    One videos.list call per 50 ids serves both."""
    blocked, durations = set(), {}
    ids = list(dict.fromkeys(video_ids))   # dedupe, preserve order
    for i in range(0, len(ids), 50):       # videos.list accepts 50 ids/call
        batch = ids[i:i + 50]
        data = yt_get('videos', {'part': 'contentDetails', 'id': ','.join(batch)})
        for item in data.get('items', []):
            cd = item.get('contentDetails', {})
            if not _viewable(cd.get('regionRestriction'), country):
                blocked.add(item['id'])
            secs = _iso_duration_secs(cd.get('duration'))
            if secs:
                durations[item['id']] = secs
    return blocked, durations

_ISO_DUR = re.compile(r'^P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$')

def _iso_duration_secs(s):
    """'PT2H13M5S' → 7985. None if the string doesn't parse (live streams
    report 'P0D' — that parses to 0, which the caller treats as unknown)."""
    m = _ISO_DUR.match(s or '')
    if not m:
        return None
    d, h, mi, se = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + se

def get_video_durations(video_ids):
    """{video_id: seconds}. Ids YouTube doesn't return (deleted) or whose
    duration is 0/unparseable (upcoming/live streams) are absent — callers
    treat absent as 'unknown', not 'short'."""
    return get_video_details(video_ids)[1]

# ── Title parsing ─────────────────────────────────────────────────────────────

# Title segment delimiters: pipes, slashes, and a spaced dash — Bellator tags
# events as '… vs. Brandon Girtz 3 - Bellator 181'. Spaced only, so hyphenated
# names ('Taylor-Melendez') survive.
_SEG_SPLIT = re.compile(r'[|/]|\s+[-–—]\s+')

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
    for seg in _SEG_SPLIT.split(title):
        seg = re.sub(r'\([^)]*\)', '', seg)         # drop parentheticals
        # Fold accents first ('Patrício' -> 'Patricio') so the non-ASCII strip
        # below only removes CJK/emoji, not letters inside Latin names.
        seg = unicodedata.normalize('NFKD', seg)
        seg = ''.join(c for c in seg if not unicodedata.combining(c))
        seg = re.sub(r'[^\x00-\x7F]+', '', seg)     # drop non-ASCII (CJK names)
        # Drop a trailing rematch marker — '… 2', '… II', '… I' (Bellator
        # labels first meetings 'I'). The ordinal itself is recovered by
        # rematch_ordinal() to pick the right bout of a series.
        seg = re.sub(r'\s+(?:\d+|[IVX]+)\s*$', '', seg).strip()
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

_ORDINAL = {'i': 1, 'ii': 2, 'iii': 3, 'iv': 4, 'v': 5, 'vi': 6}

def rematch_ordinal(title):
    """Which bout of a series a title names — 'Carvalho v Manhoef II' -> 2,
    'Straus vs. Pitbull 4' -> 4, 'Davis vs. McGeary I' -> 1; None if untagged.
    Read off the A-vs-B segment only, so an event number elsewhere in the
    title ('… | Bellator 238') is never mistaken for an ordinal."""
    for seg in _SEG_SPLIT.split(title):
        if not re.search(r'\s+vs?\.?\s+', seg, re.IGNORECASE):
            continue
        seg = re.sub(r'\([^)]*\)', '', seg).strip(' !?.')
        m = re.search(r'\s+(\d{1,2}|[ivx]+)$', seg, re.IGNORECASE)
        if m:
            tok = m.group(1).lower()
            return int(tok) if tok.isdigit() else _ORDINAL.get(tok)
    return None

def playlist_event_hint(playlist_title):
    """'PFL Paris: Full Fight Videos' -> 'pfl paris'. Empty for catch-all
    playlists whose title is just the marker ('Full Fights | Bellator MMA')."""
    name = re.split(r'\s*:?\s*full fight', playlist_title, flags=re.IGNORECASE)[0]
    return name.strip().lower()

# Filler that can share a segment with the event tag ('Bellator 301 Full
# Fight', 'Full Event Re-Air') — stripped before the segment is judged.
_HINT_FILLER = re.compile(
    r'\b(?:ful?l\s+fights?(?:\s+videos?)?|full\s+event|re-?air|no\s+comms?)\b[!]*',
    re.IGNORECASE)
# Segments that are never an event tag: hashtag spam and the A-vs-B matchup
# (handled by parse_fight_candidates).
_NOT_HINT_SEG = re.compile(r'^\s*#|\s+vs?\.?\s+', re.IGNORECASE)

def title_event_hint(title):
    """Per-video event tag for channels whose playlists don't name the event —
    Bellator files everything in one 'Full Fights' list but tags each title:
      'CYBORG ARRIVES! | Cris Cyborg v Julia Budd | Full Fight | Bellator 238'
        -> 'bellator 238'
      'Raufeon Stots vs Danny Sabatello | Bellator 301 Full Fight'
        -> 'bellator 301'
    Walks the pipe segments from the end, strips marker filler, and returns
    the first segment that still has content and isn't the matchup; '' if
    none. A hype segment ('HEAD KICK KO!') can come back when the title has
    no tag — harmless, it matches no event name."""
    for seg in reversed(_SEG_SPLIT.split(title)):
        seg = re.sub(r'[^\x00-\x7F]+', '', seg)
        seg = _HINT_FILLER.sub(' ', seg).strip(' -–—:!?.')
        if seg and not _NOT_HINT_SEG.search(seg):
            return seg.lower()
    return ''

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
        # name → [ids]: distinct fighters can share a full name — all ids
        # become pair candidates; the fight lookup disambiguates.
        fighter_by_name.setdefault(name.lower(), []).append(fid)
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
    ids = by_name.get(nl)   # list — same-name fighters all qualify
    if ids:
        return ids
    fid = by_alias.get(nl)
    if fid:
        return [fid]
    return by_last.get(surname(nl), [])

def _pair_candidates(ids_a, ids_b, fight_lookup):
    out = []
    for a in ids_a:
        for b in ids_b:
            out.extend(fight_lookup.get(frozenset((a, b)), []))
    return out

def find_fight(name_a, name_b, event_hint, lookups, ordinal=None):
    (by_name, by_last, by_alias, fighter_names, event_by_id, fight_lookup) = lookups

    ids_a = resolve_fighter(name_a, by_name, by_alias, by_last)
    ids_b = resolve_fighter(name_b, by_name, by_alias, by_last)
    if not ids_a:
        return None, f'fighter not found: "{name_a}"'
    if not ids_b:
        return None, f'fighter not found: "{name_b}"'

    candidates = _pair_candidates(ids_a, ids_b, fight_lookup)
    if not candidates:
        # An exact full-name hit can be the WRONG namesake ('Chris Gonzalez'
        # is two fighters, neither the Bellator 'Christopher Gonzalez').
        # Widen both sides to every fighter with that surname — the pair
        # lookup still has to find an actual bout between them.
        loose_a = sorted(set(ids_a) | set(by_last.get(surname(name_a), [])))
        loose_b = sorted(set(ids_b) | set(by_last.get(surname(name_b), [])))
        candidates = _pair_candidates(loose_a, loose_b, fight_lookup)
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

    # Multiple (rematch) → disambiguate by the event hint (playlist title or
    # the video title's trailing event tag). Every hint token must appear as
    # a whole word in the event name — token-wise so 'bellator 58' finds
    # 'BFC - Bellator Fighting Championships 58', whole-word so 'bellator 23'
    # can't hit 'Bellator 238'.
    if event_hint:
        toks = [t for t in re.split(r'[^a-z0-9]+', event_hint.lower()) if t]
        def _hint_hits(ev_name):
            words = set(re.split(r'[^a-z0-9]+', ev_name.lower()))
            return bool(toks) and all(t in words for t in toks)
        hits = [c for c in candidates
                if _hint_hits(event_by_id.get(c['event_id'], {}).get('name', ''))]
        if len(hits) == 1:
            return hits[0], 'matched (by event)'

    # Series ordinal ('Carvalho v Manhoef II' → the 2nd meeting). Sherdog
    # holds every pro bout, so the candidate list IS the series; sort by date
    # and take the Nth. An ordinal past the end means the DB is missing a bout
    # (or the title is wrong) — refuse rather than guess.
    if ordinal and ordinal <= len(candidates):
        ordered = sorted(candidates,
                         key=lambda c: (event_by_id.get(c['event_id'], {}).get('date') or ''))
        return ordered[ordinal - 1], f'matched (bout {ordinal} of {len(candidates)})'

    return None, f'ambiguous ({len(candidates)} fights): {name_a} vs {name_b}'

# Markers that identify a highlights / recap / promo clip rather than a genuine
# full-event upload. A title like 'BRAVE 100 ... | Full Event Highlights' carries
# 'full event' but is NOT one — reject it so it never attaches to an event.
_NOT_FULL_EVENT = re.compile(
    r'\b(highlights?|recap|vlog|preview|promo|trailer|teaser|'
    # Fight-week streams — LFA's 'LFA 229 *live* weigh-ins' runs 36 min and
    # a long presser would clear the duration gate.
    r'weigh-?ins?|press\s+conference|face-?offs?|media\s+day)\b', re.IGNORECASE)

# Roman numerals 2-10 → arabic. ONE files its themed cards as 'FISTS OF FURY II'
# on YouTube but 'Fists of Fury 2' on Sherdog; the numeral is often the only
# thing separating an event from its sibling, so it must survive tokenization.
# 'i' (=1) is deliberately omitted — a lone 'i' is too collision-prone and a
# base event has no number anyway ('Fists of Fury', not 'Fists of Fury 1').
_ROMAN = {'ii': '2', 'iii': '3', 'iv': '4', 'v': '5', 'vi': '6',
          'vii': '7', 'viii': '8', 'ix': '9', 'x': '10'}

# YouTube-title org spellings folded to the DB org token before matching, so
# the numbered-event tier sees 'LFA 3' in 'LFA MMA 3 | *Full Event* ...' and
# 'Legacy FC 57' in 'LEGACY FIGHTING CHAMPIONSHIP 57'.
_TITLE_ORG_ALIASES = [
    (re.compile(r'\blegacy\s+fighting\s+alliance\b', re.I), 'LFA'),
    (re.compile(r'\blegacy\s+fighting\s+championships?\b', re.I), 'Legacy FC'),
    (re.compile(r'\bLFA\s+(?:MMA|Fighting)\s+(\d+)\b', re.I), r'LFA \1'),
]

_NUMBERED_NAME = re.compile(r'^\s*(.+?)\s+(\d+)\b')

def _numbered_key(event_name):
    """('lfa', '241') for 'LFA 241 - Pires vs Pereira'; None when the name
    doesn't lead with '<words> <number>'. Prefix is lowercased with
    punctuation folded to single spaces, matching the title normalization
    in match_full_event."""
    m = _NUMBERED_NAME.match(event_name or '')
    if not m:
        return None
    prefix = ' '.join(re.split(r'[^a-z0-9]+', m.group(1).lower())).strip()
    return (prefix, m.group(2)) if prefix else None

def _is_full_event_title(title):
    """A video that is a full card replay — '... | Full Event' (main card) or
    '... Full Prelim(s)' (LFA's prelim-half uploads) — rather than a single
    bout. Both routes send such titles to match_full_event."""
    # Bellator's channel labels whole-card replays 'Re-Air' with no other
    # marker ('Re-Air | Bellator 195 Caldwell vs. Higo'); LFA's prelim halves
    # are 'LFA 241 Prelims Card *live* | 3 x Live Prelims'.
    return bool(re.search(r'\bfull (?:event|card|prelims?)\b|\bre-?air\b'
                          r'|\bprelims?\s+card\b',
                          title, re.IGNORECASE))

def _full_card_variant(title, default='main'):
    """'prelims' for prelim-card replays, 'main' for main-card/full-event
    replays, else `default` — the playlist's nature when the title says
    neither ('LFA 204: | Reylan Gracie pro debut | ...' inside the prelim
    replay list). Routed to events.youtube_prelims_url / events.youtube_url."""
    if re.search(r'\bprelim(?:s|inary)?\b', title, re.IGNORECASE):
        return 'prelims'
    if re.search(r'\b(?:main|full)\s+card\b|\bfull\s+event\b|\bre-?air\b',
                 title, re.IGNORECASE):
        return 'main'
    return default

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

    for rx, repl in _TITLE_ORG_ALIASES:
        title = rx.sub(repl, title)

    # Tier 0 — numbered event. Most orgs name cards '<Org> <N> - A vs. B', and
    # the '<Org> <N>' pair is the whole identity: 'LFA 241 Main Card *LIVE* |
    # 6 x Fight Full Card | LFA Fights' is LFA 241 no matter how much filler
    # surrounds it, and 'Re-Air | Bellator 196 & Kickboxing 9' is Bellator
    # 196. Whole-word on both sides so 'LFA 1' can't hit 'LFA 18'. Only when
    # exactly one in-scope event owns that pair; otherwise fall through to
    # the token tiers.
    tl = ' ' + re.sub(r'[^a-z0-9]+', ' ', title.lower()) + ' '
    numbered = []
    for e in events:
        key = _numbered_key(e['name'])
        if key and f' {key[0]} {key[1]} ' in tl:
            numbered.append(e)
    if len(numbered) == 1:
        return numbered[0], 'matched (numbered event)'

    # Two candidate keys, tried in order: (1) everything before the full-card
    # marker; (2) just the first pipe-segment — LFA slots hype filler between
    # the event name and the marker ('LFA 234 | TWO world title fight | Full
    # Card | ...'), and the first segment alone carries the identity.
    raw_keys = [re.split(r'\|?\s*full (?:event|card|prelims?)\b', title,
                         maxsplit=1, flags=re.IGNORECASE)[0]]
    first_seg = title.split('|')[0]
    if first_seg.strip() and first_seg not in raw_keys:
        raw_keys.append(first_seg)
    # (3) every other pipe segment on its own — Bellator's re-airs put hype
    # first and the event tag anywhere ('HEAVYWEIGHT MAYHEM! | Bellator 52
    # Full Event | When Heavyweights OWN the Main Card', 'Rampage Jackson,
    # King Mo & MORE!! | Bellator 110 | Full Event Re-Air'). Marker filler
    # ('Full Event', 'Re-Air') is stripped so 'Bellator 52 Full Event' keys
    # as 'Bellator 52'.
    for seg in title.split('|')[1:]:
        seg = _HINT_FILLER.sub(' ', seg).strip()
        if seg and seg not in raw_keys:
            raw_keys.append(seg)

    last_reason = 'no key tokens'
    for key in raw_keys:
        key = re.sub(r'@\S+', ' ', key)          # drop @collab handles
        key = _HINT_FILLER.sub(' ', key)         # 'Re-Air', 'Full Fight' …
        # ' v ' is 'vs', not the roman numeral 5 ('Bellator v Rizin 2').
        key = re.sub(r'\s+v\s+', ' vs ', key, flags=re.IGNORECASE)
        # 'Bellator 221 Full Event Feat. Page, Lima & MORE!' — the line-up
        # after 'feat' is filler, not identity.
        key = re.sub(r'\bfeat\.?\b.*$', ' ', key, flags=re.IGNORECASE)
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

        exact, num_hits, rev_hits, name_hits, hits = [], [], [], [], []
        for e in events:
            if year and year not in (e.get('date') or ''):
                continue
            org  = set(re.split(r'[^a-z0-9]+', (e.get('organization') or '').lower()))
            name = set(_sig_tokens(e['name']))
            loc  = set(re.split(r'[^a-z0-9]+', (e.get('location') or '').lower()))
            v = vid - org      # org name is implied by scope — don't require it
            if not v:
                continue
            # Reverse subset: the EVENT's tokens all appear in the video key
            # and the numbers agree — for re-airs titled with full names
            # ('Bellator 182 Andrey Koreshkov vs. Chidi Njokuani' against
            # 'Bellator 182 - Koreshkov vs. Njokuani').
            # Guarded: when the event name is just '<Org> <N>' ('Legacy FC
            # 31') its non-org tokens are only the number, and any title
            # carrying that number would qualify — 'ATTITUDE MMA 31 *FULL*
            # EVENT', 'RFA 33 * FULL EVENT*' (promotions not in the DB). So
            # the org must then appear in the title too.
            name_core = name - org
            if (name_core <= v
                    and {t for t in v if t.isdigit()} == {t for t in name if t.isdigit()}
                    and (any(not t.isdigit() for t in name_core) or (org & vid))):
                rev_hits.append(e)
            if v == (name - org):
                exact.append(e)
            if v <= name:
                name_hits.append(e)
                # Same numbers on both sides: 'Bellator 4' must not also
                # satisfy 'Bellator 178 - Straus vs. Pitbull 4'.
                if {t for t in v if t.isdigit()} == {t for t in name if t.isdigit()}:
                    num_hits.append(e)
            if v <= (name | loc):
                hits.append(e)

        label = f'{year or "any"} / {"+".join(sorted(vid))}'
        if len(exact) == 1:
            return exact[0], 'matched (exact)'
        if len(num_hits) == 1:
            return num_hits[0], 'matched (numbers)'
        if len(rev_hits) == 1:
            return rev_hits[0], 'matched (event tokens in title)'
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
    aliases  = fetch_all('fighter_aliases', 'alias,fighter_id', order='alias')
    all_fights = fetch_all('fights', 'id,event_id,fighter1_id,fighter2_id')

    matched, unmatched = [], []
    ev_matched, ev_unmatched = [], []
    seen_fight = set()
    seen_event = set()

    def record_full_event(vid, vtitle, events, default_variant='main'):
        """Match a full-card video ('Full Event' main card or 'Full Prelim')
        to one of `events` and record it, deduped by (event, variant). Used
        from both the playlist scan (ONE files full events inside per-fight
        playlists) and the uploads scan (PFL, LFA). `default_variant` is the
        card half assumed when the title names neither."""
        url = VIDEO_URL_TMPL.format(vid)
        event, reason = match_full_event(vtitle, events)
        if not event:
            ev_unmatched.append((reason, vtitle, url))
            return
        # A scheduled live stream ('LFA 241 Main Card *LIVE*') is listed days
        # ahead of the card. Don't attach it until the event has happened —
        # the next daily run picks it up once the date passes.
        if _is_future_event(event):
            ev_unmatched.append((f'event not yet held ({event["name"]})', vtitle, url))
            return
        variant = _full_card_variant(vtitle, default_variant)
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

        # Full-card candidates are collected per channel and matched after a
        # duration check (one videos.list per 50) — see MIN_FULL_CARD_SECS.
        card_pending = []

        card_ids = dict(ch.get('card_playlists', {}))   # id → default variant
        playlists = get_full_fight_playlists(
            ch['channel_id'], extra_ids=list(ch.get('fight_playlists', [])) + list(card_ids))
        print(f'  {len(playlists)} playlists to scan')

        fight_ids = set(ch.get('fight_playlists', []))
        for pl in playlists:
            # Hand-picked playlists are themed ('BEFORE THE UFC'), not named
            # for an event — leave the hint empty so each video's own tag
            # ('... | LFA 146') disambiguates rematches instead.
            hint = '' if pl['id'] in fight_ids else playlist_event_hint(pl['title'])
            videos = get_playlist_videos(pl['id'])
            is_card_list = pl['id'] in card_ids
            print(f'    [{pl["title"]}] — {len(videos)} videos'
                  + (' (full cards)' if is_card_list else ''))
            for vid, vtitle in videos:
                url = VIDEO_URL_TMPL.format(vid)
                # A playlist can hold event replays alongside single bouts
                # (ONE's themed 'Full Fights' playlists include a '... | Full
                # Event' video). Route those to the full-event matcher, not the
                # A-vs-B parser (which would just PARSE_FAIL on them). In a
                # channel's designated card playlists every video is a
                # full-card candidate — LFA's prelim replays carry no marker
                # at all ('LFA 162 Prelims | Unbeaten Michael Oliveira vs ...').
                if is_card_list or _is_full_event_title(vtitle):
                    card_pending.append((vid, vtitle, card_ids.get(pl['id'], 'main')))
                    continue
                # Highlight reels share the A-vs-B shape ('Ahmet Kayretli vs
                # Andrew Salas | LFA 49 Fight Highlight') and would otherwise
                # be stored as the bout's video.
                if _NOT_FULL_EVENT.search(vtitle) and not re.search(
                        r'\bfull\s+fight', vtitle, re.IGNORECASE):
                    unmatched.append(('highlight/recap clip', vtitle, url))
                    continue
                cand_pairs = parse_fight_candidates(vtitle)
                if not cand_pairs:
                    unmatched.append(('PARSE_FAIL', vtitle, url))
                    continue

                # Playlist-level hint first; fall back to the video's own
                # trailing event tag for catch-all playlists.
                vhint = hint or title_event_hint(vtitle)
                ordinal = rematch_ordinal(vtitle)
                fight = None
                reason = 'no candidate matched'
                for name_a, name_b in cand_pairs:
                    fight, reason = find_fight(name_a, name_b, vhint, lookups, ordinal)
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
        card_pending.extend((v, t, 'main') for v, t in fe_videos)

        # Duration gate, then match. Unknown durations (upcoming/live
        # streams, deleted videos) pass through — the title tiers and the
        # future-event guard still apply to them.
        seen_pending = set()
        durations = get_video_durations([v for v, _, _ in card_pending])
        short = 0
        for vid, vtitle, default_variant in card_pending:
            if vid in seen_pending:
                continue
            seen_pending.add(vid)
            secs = durations.get(vid)
            if secs is not None and secs < MIN_FULL_CARD_SECS:
                short += 1
                ev_unmatched.append((f'too short for a full card ({secs // 60} min)',
                                     vtitle, VIDEO_URL_TMPL.format(vid)))
                continue
            record_full_event(vid, vtitle, events, default_variant)
        print(f'    {len(seen_pending)} full-card candidates matched '
              f'({short} rejected as too short)')

    # 'events' is per-channel above; for the geo-block + write-file passes
    # below we use a unified event_by_id keyed off matched results.
    events = fetch_all('events', 'id,name,date,location')

    # ── Drop videos geo-blocked in COUNTRY ────────────────────────────────────
    print(f'\nChecking region restrictions ({COUNTRY})...')
    blocked_ids, durations = get_video_details(
        [m['video_id'] for m in matched] + [m['video_id'] for m in ev_matched])
    # A single bout never runs 40+ min; a 'fight' video that long is a card
    # stream whose title happened to name one matchup ('LFA 204: | Reylan
    # Gracie pro debut | Kyle Pufahl vs Kevin Carrier'). Don't pin it on the
    # bout.
    long_fights = [m for m in matched
                   if durations.get(m['video_id'], 0) >= MIN_FULL_CARD_SECS]
    for m in long_fights:
        unmatched.append((f'multi-fight video ({durations[m["video_id"]] // 60} min)',
                          m['video_title'], m['url']))
    matched = [m for m in matched if m not in long_fights]
    if long_fights:
        print(f'  {len(long_fights)} fight videos dropped as multi-fight streams')
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
    parser.add_argument('--channel', metavar='NAME',
                        help='Only scan the CHANNELS entry with this name '
                             '(e.g. Bellator) — for testing a new channel '
                             'without spending quota on the rest')
    args = parser.parse_args()
    if args.channel:
        CHANNELS[:] = [c for c in CHANNELS if c['name'].lower() == args.channel.lower()]
        if not CHANNELS:
            sys.exit(f'No CHANNELS entry named {args.channel!r}')
    main(push=args.push)
