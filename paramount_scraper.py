#!/usr/bin/env python3
"""
paramount_scraper.py
Fetches UFC fight clips and full-event links from Paramount+ and matches them
to fights/events in Supabase.

Shows scraped:
  ufc-fight-night  — individual fight clips for Fight Night events
  ufc              — numbered events: full-card entries + individual fight clips
  ufc-2010s        — 2010s event recaps  → events.paramount_url
  ufc-2000s        — 2000s event recaps  → events.paramount_url

Results are saved to paramount_matches.txt for review before any DB changes.
"""

import os
import re
import sys
import time
import json
import subprocess
import requests
from datetime import datetime

# Windows consoles default to cp1252 which can't render the U+2192 arrow
# (and other Unicode glyphs) the scraper prints. Force UTF-8.
try:
    sys.stdout.reconfigure(encoding='utf-8')
except (AttributeError, ValueError):
    pass

# ── Load .env ─────────────────────────────────────────────────────────────────
_env_path = os.path.join(os.path.dirname(__file__), '.env')
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith('#') and '=' in _line:
                _k, _v = _line.split('=', 1)
                os.environ.setdefault(_k.strip(), _v.strip())

SUPABASE_URL      = os.environ.get('SUPABASE_URL', '').rstrip('/')
SUPABASE_KEY      = os.environ.get('SUPABASE_KEY', '')
PARAMOUNT_COOKIES = os.environ.get('PARAMOUNT_COOKIES', '')
PARAMOUNT_BASE    = 'https://www.paramountplus.com'
OUTPUT_FILE       = 'paramount_matches.txt'

# Show slugs to scrape.  Each entry: (slug, seasons, show_type)
#   show_type: 'fight_night' | 'numbered' | 'recap'
SHOW_CONFIGS = [
    ('ufc-fight-night', range(2006, 2027), 'fight_night'),
    ('ufc',             range(2000, 2027), 'numbered'),
    ('ufc-2010s',       range(2010, 2021), 'recap'),
    ('ufc-2000s',       range(2000, 2010), 'recap'),
]

# ── Supabase ──────────────────────────────────────────────────────────────────

def supabase_get(table, params=''):
    """Paginated fetch — Supabase caps each response at 1000 rows. Callers
    can still pass `limit=N` in params to cap the total; without it we walk
    all rows in 1000-row pages."""
    PAGE, offset, out = 1000, 0, []
    headers = {
        'apikey': SUPABASE_KEY,
        'Authorization': f'Bearer {SUPABASE_KEY}',
    }
    while True:
        sep = '&' if params else ''
        url = f'{SUPABASE_URL}/rest/v1/{table}?{params}{sep}limit={PAGE}&offset={offset}'
        resp = requests.get(url, headers=headers)
        if not resp.ok:
            return out
        batch = resp.json()
        out.extend(batch)
        if len(batch) < PAGE:
            return out
        offset += PAGE

def _sb_headers():
    return {
        'apikey': SUPABASE_KEY,
        'Authorization': f'Bearer {SUPABASE_KEY}',
        'Content-Type': 'application/json',
        'Prefer': 'return=minimal',
    }

def bulk_update_urls(rpc_name, records, id_key, url_key='paramount_url'):
    """Call a Supabase sproc that bulk-updates URLs. Returns count updated."""
    payload = [{'id': r[id_key], 'url': r[url_key]} for r in records]
    resp = requests.post(
        f'{SUPABASE_URL}/rest/v1/rpc/{rpc_name}',
        headers=_sb_headers(),
        data=json.dumps({'data': payload}),
    )
    if resp.status_code in (200, 204):
        try:
            return int(resp.json())
        except Exception:
            return len(records)
    print(f'  [WARN] {rpc_name} failed ({resp.status_code}): {resp.text[:200]}')
    return 0

# ── Paramount+ ────────────────────────────────────────────────────────────────

def _curl_fetch(url):
    """Fetch a URL via curl and return parsed JSON, or None on failure."""
    result = subprocess.run(
        [
            'curl', '-s', url,
            '-H', 'accept: application/json, text/javascript, */*; q=0.01',
            '-H', 'accept-language: en-US,en;q=0.9',
            '-H', f'referer: {PARAMOUNT_BASE}/',
            '-H', 'x-requested-with: XMLHttpRequest',
            '-H', 'sec-ch-ua: "Chromium";v="146", "Not-A.Brand";v="24", "Microsoft Edge";v="146"',
            '-H', 'sec-ch-ua-mobile: ?0',
            '-H', 'sec-ch-ua-platform: "Windows"',
            '-H', 'sec-fetch-dest: empty',
            '-H', 'sec-fetch-mode: cors',
            '-H', 'sec-fetch-site: same-origin',
            '-H', 'user-agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36 Edg/146.0.0.0',
            '-b', PARAMOUNT_COOKIES,
        ],
        capture_output=True, text=True, timeout=30,
    )
    return json.loads(result.stdout)

def fetch_clips_for_show(slug, seasons):
    """Fetch all clips for a show across all seasons, paginating with size=18."""
    PAGE_SIZE = 18
    all_clips = []
    for season in seasons:
        page = 0
        season_clips = []
        while True:
            url = (f'{PARAMOUNT_BASE}/shows/{slug}/xhr/episodes'
                   f'/page/{page}/size/{PAGE_SIZE}/xs/0/season/{season}/')
            try:
                data  = _curl_fetch(url)
                clips = data.get('result', {}).get('data', [])
                if not clips:
                    break
                season_clips.extend(clips)
                if len(clips) < PAGE_SIZE:
                    break
                page += 1
                time.sleep(0.2)
            except Exception as e:
                print(f'  [{slug}] {season} p{page}: error — {e}')
                break
        if season_clips:
            print(f'  [{slug}] {season}: {len(season_clips)} clips')
            all_clips.extend(season_clips)
        else:
            print(f'  [{slug}] {season}: (no clips)')
        time.sleep(0.3)
    return all_clips

# ── Clip classification ───────────────────────────────────────────────────────

def classify_clip(title):
    """
    Returns one of:
      'recap'       — title contains "Event Recap" → events.paramount_url
      'fight_clip'  — title contains "vs."         → fights.paramount_url
      'unknown'     — everything else (skipped)
    """
    tl = title.lower()
    if 'event recap' in tl or re.search(r'\ball the fights\b', tl):
        return 'recap'
    if re.search(r'\bvs\.?\s', tl):
        return 'fight_clip'
    return 'unknown'

def extract_event_name_from_clip(title, clip_type):
    """
    Pull the clean event name out of a clip title.

    full_event:  "UFC 326: Holloway vs. Oliveira 2 - All The Fights"
                 → "UFC 326: Holloway vs. Oliveira 2"

    recap:       "UFC 200 Event Recap"  → "UFC 200"
                 "UFC 189 Recap"        → "UFC 189"
    """
    if clip_type == 'full_event':
        return re.sub(r'\s*-\s*all the fights.*', '', title, flags=re.IGNORECASE).strip()

    if clip_type == 'recap':
        # Strip "Event Recap", "Recap", or "- All The Fights" suffix
        name = re.sub(r'\s*-?\s*\b(all the fights|event recap|recap)\b.*', '', title, flags=re.IGNORECASE).strip()
        # Bare number prefix "321: Aspinall vs. Gane" → "UFC 321"
        m = re.match(r'^(\d+)\s*:', name)
        if m:
            return f'UFC {m.group(1)}'
        return name

    return None

# ── Date helpers ──────────────────────────────────────────────────────────────

def parse_event_date(date_str):
    """Parse 'April 13, 2024', 'Apr 13, 2024', or 'YYYY-MM-DD' -> datetime."""
    if not date_str:
        return None
    for fmt in ('%B %d, %Y', '%b %d, %Y'):
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    try:
        return datetime.strptime(date_str[:10], '%Y-%m-%d')
    except ValueError:
        return None

def parse_clip_date(clip):
    """Parse airdate_iso from clip -> datetime (date only)."""
    try:
        return datetime.fromisoformat(clip.get('airdate_iso', '')[:10])
    except Exception:
        return None

# ── Parsing ───────────────────────────────────────────────────────────────────

def parse_fighters(title):
    """
    Extract (fighter_a, fighter_b) from a Paramount+ clip title.

    Handles formats:
      "Ethyn Ewing vs. Rafael Estevam (UFC Fight Night: ...)"
      "Cornolle vs. Edwards Highlights - Fight Night"
    """
    # Strip parenthetical event info
    title = re.sub(r'\s*\(.*?\)', '', title).strip()
    # Strip " Highlights - ..." suffix
    title = re.sub(r'\s+Highlights.*', '', title, flags=re.IGNORECASE).strip()
    # Strip card section / recap suffix — with or without a leading dash
    # e.g. "- Main", "Main Card", " Prelims", "Event Recap"
    title = re.sub(r'\s*-?\s*(Main Card|Main|Prelims?|Event Recap)\s*$', '', title, flags=re.IGNORECASE).strip()
    # Strip leading event-number prefix: "326: " or "UFC 235: "
    title = re.sub(r'^(?:UFC\s+)?\d+\s*:\s*', '', title).strip()

    m = re.match(r'^(.+?)\s+vs\.?\s+(.+)$', title, re.IGNORECASE)
    if not m:
        return None, None
    return m.group(1).strip(), m.group(2).strip()

def parse_card_section(title):
    """Return 'Main Card', 'Prelims', or None from title suffix."""
    if re.search(r'\bMain Card\b', title, re.IGNORECASE):
        return 'Main Card'
    if re.search(r'\bPrelims?\b', title, re.IGNORECASE):
        return 'Prelims'
    return None

def extract_ufc_number(title):
    """Extract the event number from 'UFC 326: ...' → 326, or None."""
    m = re.match(r'^UFC\s+(\d+)\b', title, re.IGNORECASE)
    return int(m.group(1)) if m else None

def fetch_event_fight_clips(ufc_number):
    """
    Fetch individual fight clips for a numbered UFC event via the
    /shows/ufc/video/xhr/episodes/ endpoint (season = UFC event number).
    """
    PAGE_SIZE = 18
    all_clips = []
    page = 0
    while True:
        url = (f'{PARAMOUNT_BASE}/shows/ufc/video/xhr/episodes'
               f'/page/{page}/size/{PAGE_SIZE}/xs/0/season/{ufc_number}/')
        try:
            data  = _curl_fetch(url)
            clips = data.get('result', {}).get('data', [])
            if not clips:
                break
            all_clips.extend(clips)
            if len(clips) < PAGE_SIZE:
                break
            page += 1
            time.sleep(0.2)
        except Exception as e:
            print(f'  [ufc/{ufc_number}] p{page}: error — {e}')
            break
    return all_clips

def parse_event_hint(title):
    """
    Extract the event name embedded in a clip title.

    Pattern 1 — parenthetical (numbered-event show):
      "Max Holloway vs. Charles Oliveira (UFC 326: Holloway vs. Oliveira 2 - Main)"
      → "UFC 326: Holloway vs. Oliveira 2"

    Pattern 2 — title prefix (recap shows):
      "UFC 117: Anderson Silva vs. Chael Sonnen"
      → "UFC 117"  (prefix match handled by find_event)

    Returns None if no event hint is found.
    """
    # Pattern 1: parenthetical
    m = re.search(r'\((.+?)\)', title)
    if m:
        inner = m.group(1)
        inner = re.sub(r'\s*-\s*(Main|Prelims?)\s*$', '', inner, flags=re.IGNORECASE).strip()
        return inner or None

    # Pattern 2: "UFC NNN: ..." prefix — return "UFC NNN" for prefix matching
    m = re.match(r'^(UFC\s+\d+)\s*:', title, re.IGNORECASE)
    if m:
        return m.group(1)

    # Pattern 3: bare number prefix "326: ..." or "321: Fighter vs. Fighter Main Card"
    m = re.match(r'^(\d+)\s*:', title)
    if m:
        return f'UFC {m.group(1)}'

    return None

# ── Matching ──────────────────────────────────────────────────────────────────

def find_fight(name_a, name_b, clip_date, fight_lookup, fighter_by_name, fighter_by_last,
               event_by_id, event_name_hint=None, event_by_name_lower=None,
               fighter_by_alias=None):
    """
    Match a clip to a fight.
    Returns (fight, event, reason).

    event_name_hint: event name extracted from the clip title parenthetical — used
                     as the primary tiebreaker for rematches before falling back to date.
    fighter_by_alias: alias → fighter_id map for name variant resolution.
    """
    def lookup_ids(name):
        nl = name.lower()
        # 1. Exact full name
        fid = fighter_by_name.get(nl)
        if fid:
            return [fid]
        # 2. Alias table
        if fighter_by_alias:
            fid = fighter_by_alias.get(nl)
            if fid:
                return [fid]
        # 3. Last name fallback
        return fighter_by_last.get(name.split()[-1].lower(), [])

    ids_a = lookup_ids(name_a)
    ids_b = lookup_ids(name_b)

    if not ids_a or not ids_b:
        missing = []
        if not ids_a: missing.append(name_a)
        if not ids_b: missing.append(name_b)
        return None, None, f'no_fighter({", ".join(missing)})'

    candidates = []
    for id_a in ids_a:
        for id_b in ids_b:
            key = frozenset([id_a, id_b])
            candidates.extend(fight_lookup.get(key, []))

    if not candidates:
        return None, None, 'no_fight'

    # 1. Narrow by event name extracted from the clip title (best for rematches)
    if event_name_hint and event_by_name_lower and len(candidates) > 1:
        hint_ev, _ = find_event(event_name_hint, clip_date, event_by_name_lower,
                                list(event_by_id.values()))
        if hint_ev:
            ev_filtered = [f for f in candidates if f['event_id'] == hint_ev['id']]
            if ev_filtered:
                candidates = ev_filtered

    # 2. Fall back to date proximity (±3 days)
    if clip_date and len(candidates) > 1:
        dated = []
        for fight in candidates:
            ev_date = parse_event_date(event_by_id.get(fight['event_id'], {}).get('date', ''))
            if ev_date and abs((ev_date - clip_date).days) <= 3:
                dated.append(fight)
        if dated:
            candidates = dated

    if len(candidates) == 1:
        fight = candidates[0]
        return fight, event_by_id.get(fight['event_id'], {}), 'ok'
    if len(candidates) > 1:
        return None, None, f'ambiguous({len(candidates)} fights)'
    return None, None, 'no_date_match'

def find_event(raw_name, clip_date, event_by_name_lower, events):
    """
    Match an extracted event name string to an event record.
    Returns (event_record, reason).
    """
    if not raw_name:
        return None, 'no_name'

    key = raw_name.lower().strip()

    # 1. Exact match
    ev = event_by_name_lower.get(key)
    if ev:
        return ev, 'ok'

    # 2. Numbered prefix match: "ufc 326" matches "UFC 326: Holloway vs. Oliveira 2"
    m = re.match(r'^(ufc\s+\d+)', key)
    if m:
        prefix = m.group(1)
        candidates = [e for e in events if e['name'].lower().startswith(prefix)]
        if clip_date:
            dated = [e for e in candidates
                     if e.get('date') and parse_event_date(e['date']) is not None
                     and abs((parse_event_date(e['date']) - clip_date).days) <= 7]
            if dated:
                return dated[0], 'prefix+date'
        if len(candidates) == 1:
            return candidates[0], 'prefix'
        if len(candidates) > 1:
            return None, f'prefix_ambiguous({len(candidates)})'

    return None, f'no_event_match({raw_name!r})'

# ── Main ──────────────────────────────────────────────────────────────────────

def main(dry_run=False):
    # ── Load Supabase data ────────────────────────────────────────────────────
    print('Loading Supabase data...')
    fighters = supabase_get('fighters',      'select=id,name&limit=10000')
    fights   = supabase_get('fights',        'select=id,fighter1_id,fighter2_id,event_id&limit=10000')
    events   = supabase_get('events',        'select=id,name,date&limit=10000')
    aliases  = supabase_get('fighter_aliases', 'select=alias,fighter_id&limit=10000')

    fighter_by_name  = {}
    fighter_by_last  = {}
    fighter_by_alias = {a['alias'].lower(): a['fighter_id'] for a in aliases}

    for f in fighters:
        fighter_by_name[f['name'].lower()] = f['id']
        last = f['name'].split()[-1].lower()
        fighter_by_last.setdefault(last, []).append(f['id'])

    fight_lookup = {}
    for fight in fights:
        key = frozenset([fight['fighter1_id'], fight['fighter2_id']])
        fight_lookup.setdefault(key, []).append(fight)

    event_by_id         = {e['id']: e for e in events}
    event_by_name_lower = {e['name'].lower(): e for e in events}

    print(f'  {len(fighters)} fighters, {len(fights)} fights, {len(events)} events\n')

    # ── Fetch Paramount+ clips ────────────────────────────────────────────────
    print(f'PARAMOUNT_COOKIES: {len(PARAMOUNT_COOKIES)} chars, starts: {PARAMOUNT_COOKIES[:40]!r}\n')

    matched_fights  = []   # {fight_id, fighters, event, event_date, paramount_url}
    matched_events  = []   # {event_id, event_name, event_date, paramount_url}
    unmatched       = []

    # Build UFC number list from DB events — e.g. "UFC 326: ..." → 326
    ufc_numbers = set()
    for e in events:
        m = re.match(r'^UFC\s+(\d+)\b', e['name'], re.IGNORECASE)
        if m:
            ufc_numbers.add(int(m.group(1)))
    print(f'  {len(ufc_numbers)} numbered UFC events found in DB\n')

    def process_fight_clip(clip):
        """Match a fight clip and append to matched_fights / unmatched."""
        title     = clip.get('title', '')
        p_url     = PARAMOUNT_BASE + clip.get('url', '')
        clip_date = parse_clip_date(clip)
        dur_s     = clip.get('duration_raw') or 0

        name_a, name_b = parse_fighters(title)
        if not name_a:
            unmatched.append(f'{"PARSE_FAIL":<22} | {title}')
            return

        event_hint = parse_event_hint(title)
        fight, ev, reason = find_fight(
            name_a, name_b, clip_date,
            fight_lookup, fighter_by_name, fighter_by_last, event_by_id,
            event_name_hint=event_hint,
            event_by_name_lower=event_by_name_lower,
            fighter_by_alias=fighter_by_alias,
        )
        if fight:
            matched_fights.append({
                'fight_id':      fight['id'],
                'fighters':      f'{name_a} vs. {name_b}',
                'event':         ev.get('name', ''),
                'event_date':    ev.get('date', ''),
                'paramount_url': p_url,
                'card_section':  parse_card_section(title),
                'is_highlight':  bool(re.search(r'\bhighlights?\b', title, re.IGNORECASE)),
                'duration':      dur_s if isinstance(dur_s, (int, float)) else 0,
            })
        else:
            unmatched.append(
                f'{reason:<22} | {name_a} vs. {name_b}'
                f' | {clip.get("airdate", "")} | {p_url}'
            )

    for slug, seasons, _show_type in SHOW_CONFIGS:
        print(f'Fetching show: {slug}')
        clips = fetch_clips_for_show(slug, seasons)
        print(f'  → {len(clips)} total clips\n')

        for clip in clips:
            title     = clip.get('title', '')
            p_url     = PARAMOUNT_BASE + clip.get('url', '')
            clip_date = parse_clip_date(clip)

            # Skip "All The Fights" from the ufc numbered show only —
            # those events are handled via the /video/xhr/ sub-clip fetch.
            # Recap shows (ufc-2010s etc.) use them as event URLs.
            if slug == 'ufc' and re.search(r'\ball the fights\b', title, re.IGNORECASE):
                continue

            clip_type = classify_clip(title)

            # ── Recap → events table ──────────────────────────────────────────
            if clip_type == 'recap':
                raw_name = extract_event_name_from_clip(title, clip_type)
                ev, reason = find_event(raw_name, clip_date, event_by_name_lower, events)
                if ev:
                    matched_events.append({
                        'event_id':    ev['id'],
                        'event_name':  ev['name'],
                        'event_date':  ev.get('date', ''),
                        'paramount_url': p_url,
                        'clip_title':  title,
                    })
                else:
                    unmatched.append(
                        f'EVENT {reason:<30} | {title} | {clip.get("airdate", "")} | {p_url}'
                    )
                continue

            # ── Individual fight clip → fights table ─────────────────────────
            if clip_type == 'fight_clip':
                process_fight_clip(clip)
                continue

            # unknown
            unmatched.append(f'{"UNKNOWN_TYPE":<22} | {title}')

    # ── Fetch individual fights for each numbered UFC event ───────────────────
    ufc_numbers = {n for n in ufc_numbers if n >= 246}

    if ufc_numbers:
        print(f'Fetching individual fights for {len(ufc_numbers)} numbered UFC events (246+)...')
        for ufc_num in sorted(ufc_numbers):
            sub_clips = fetch_event_fight_clips(ufc_num)
            if sub_clips:
                print(f'  UFC {ufc_num}: {len(sub_clips)} fights')
            for clip in sub_clips:
                title     = clip.get('title', '')
                p_url     = PARAMOUNT_BASE + clip.get('url', '')
                clip_date = parse_clip_date(clip)
                clip_type = classify_clip(title)

                if clip_type == 'recap':
                    raw_name = extract_event_name_from_clip(title, 'recap')
                    ev, reason = find_event(raw_name, clip_date, event_by_name_lower, events)
                    if ev:
                        matched_events.append({
                            'event_id':    ev['id'],
                            'event_name':  ev['name'],
                            'event_date':  ev.get('date', ''),
                            'paramount_url': p_url,
                            'clip_title':  title,
                        })
                    else:
                        unmatched.append(
                            f'EVENT {reason:<30} | {title} | {clip.get("airdate", "")} | {p_url}'
                        )
                elif clip_type == 'fight_clip':
                    process_fight_clip(clip)
                # else: skip weigh-ins, press conferences, breakdowns, etc.
            time.sleep(0.3)

    # ── Deduplicate — prefer non-highlight title, then longest duration ───────
    def _rank(m):
        # Lower rank wins. (is_highlight=0 beats 1, longer duration beats shorter)
        return (1 if m.get('is_highlight') else 0, -(m.get('duration') or 0))

    seen_fights = {}
    for m in matched_fights:
        fid = m['fight_id']
        existing = seen_fights.get(fid)
        if existing is None or _rank(m) < _rank(existing):
            seen_fights[fid] = m
    matched_fights = list(seen_fights.values())

    seen_events = {}
    deduped_events = []
    for m in matched_events:
        if m['event_id'] not in seen_events:
            seen_events[m['event_id']] = True
            deduped_events.append(m)
    matched_events = deduped_events

    # ── Write output file ─────────────────────────────────────────────────────
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as out:
        out.write('PARAMOUNT+ MATCH RESULTS\n')
        out.write(f'Generated: {datetime.now().strftime("%Y-%m-%d %H:%M")}\n')
        out.write(
            f'Fight clips matched: {len(matched_fights)}'
            f'  |  Event links matched: {len(matched_events)}'
            f'  |  Unmatched: {len(unmatched)}\n'
        )
        out.write('=' * 80 + '\n\n')

        out.write(f'MATCHED FIGHTS ({len(matched_fights)})\n')
        out.write('-' * 80 + '\n')
        for m in matched_fights:
            section = f'  [{m["card_section"]}]' if m['card_section'] else ''
            out.write(f"fight_id:  {m['fight_id']}\n")
            out.write(f"fighters:  {m['fighters']}{section}\n")
            out.write(f"event:     {m['event']} ({m['event_date']})\n")
            out.write(f"url:       {m['paramount_url']}\n\n")

        out.write(f'\nMATCHED EVENTS ({len(matched_events)})\n')
        out.write('-' * 80 + '\n')
        for m in matched_events:
            out.write(f"event_id:  {m['event_id']}\n")
            out.write(f"event:     {m['event_name']} ({m['event_date']})\n")
            out.write(f"clip:      {m['clip_title']}\n")
            out.write(f"url:       {m['paramount_url']}\n\n")

        out.write(f'\nUNMATCHED ({len(unmatched)})\n')
        out.write('-' * 80 + '\n')
        for u in unmatched:
            out.write(u + '\n')

    print(f'Saved to: {OUTPUT_FILE}\n')

    # ── Push to Supabase ──────────────────────────────────────────────────────
    if dry_run:
        print('Dry run — skipping database updates.')
    else:
        if matched_fights:
            print(f'Pushing {len(matched_fights)} fight URLs to Supabase...')
            ok = bulk_update_urls('update_fight_paramount_urls', matched_fights, 'fight_id')
            print(f'  Updated {ok}/{len(matched_fights)} fights.')

        if matched_events:
            print(f'Pushing {len(matched_events)} event URLs to Supabase...')
            ok = bulk_update_urls('update_event_paramount_urls', matched_events, 'event_id')
            print(f'  Updated {ok}/{len(matched_events)} events.')

    print(f'\nFight clips matched:  {len(matched_fights)}')
    print(f'Event links matched:  {len(matched_events)}')
    print(f'Unmatched:            {len(unmatched)}')

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true',
                        help='Write results to txt only, skip database updates')
    args = parser.parse_args()
    main(dry_run=args.dry_run)
