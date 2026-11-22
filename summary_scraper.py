#!/usr/bin/env python3
"""
summary_scraper.py — a short written summary of each fight into
`fight_summaries`, written by a Claude model from Sherdog's round-by-round
play-by-play article for the card.

The app shows the summary behind a "Summary" toggle on the fight row, only
after the viewer has rated the fight (it gives the result away).

Source. `events.id` is the Sherdog event id, so
https://www.sherdog.com/events/x-<id> redirects to the event page. A card
Sherdog covered live has a "VIEW PLAY-BY-PLAY" link there to a news article
whose `#playbyplay` block holds one <h2> per bout ("A (156) vs. B (155.5)")
followed by a paragraph per round and "The Official Result". The whole
article goes to the model in ONE request per event, together with the list of
that card's fights (id, names, official result from the DB); the model
returns a 2–4 sentence summary per fight id, or marks a fight as not covered.
Letting the model line bouts up with fight ids avoids name matching here
(Sherdog's "Heili Alateng" is the DB's "Alatengheili"). Only the generated
summary is stored, never the play-by-play text.

Scope. UFC by default (organization id 2; --org-ids for others). Nightly it
looks at events from the last --days (14) and only at fights that have a
result but no summary yet, so a normal night is zero or one model request.
A card with no play-by-play link is skipped (and re-checked each night while
it is inside the window). No tracker file — state is the DB.

Model. `claude-sonnet-5-5` (SUMMARY_MODEL in .env overrides), structured JSON
output. Needs ANTHROPIC_API_KEY in .env; without it the script says so and
exits 0 so the nightly doesn't fail.

  python summary_scraper.py                  # nightly: recent UFC cards lacking summaries
  python summary_scraper.py --event 114293   # one event by Sherdog id (any org, any date)
  python summary_scraper.py --all --limit 20 # backfill: every in-scope event, 20 model requests at a time
  python summary_scraper.py --force          # rewrite summaries that already exist
  python summary_scraper.py --dry-run        # fetch + parse (+ summarise if a key is set), print, write nothing
"""
import os
import re
import sys
import json
import time
import argparse
import datetime as dt

import requests
from bs4 import BeautifulSoup

import sherdog_scraper as s
from verdict_scraper import fetch_all, fold

MODEL = os.environ.get('SUMMARY_MODEL', 'claude-sonnet-5-5')
EFFORT = 'medium'
UFC_ORG_ID = 2
DELAY = 1.0
MIN_ARTICLE_CHARS = 1500      # shorter than this = the pre-event stub, no rounds yet

SYSTEM = """You write fight summaries for MMA Rewatch, an app where people rate MMA fights they have watched and decide which ones to rewatch.

Each summary is shown on a fight's row, behind a toggle, only after the user has already rated that fight. So the reader knows the result and has probably seen the fight. They want a quick reminder of how it played out: who controlled what, the moments where it turned, and how it ended.

You are given one event's round-by-round play-by-play article and the list of fights that need a summary, each with its id and the official result from the app's database.

For each listed fight:
- Find its bout in the article. Names can be spelled or ordered differently between the article and the list; use the opponents and the result to line them up.
- Write 2 to 4 sentences, about 40 to 75 words, in plain past-tense prose. Refer to fighters by surname. Say how the fight unfolded and how it ended; for a decision, say what won it rather than listing scores.
- Use only what the article says. Do not add records, rankings, odds, background or opinion, and do not copy the article's sentences; describe the action in your own words.
- A draw or a no contest is an ordinary result: summarise the fight and say how it came to end without a winner (for example a point deduction that levelled the scorecards).
- The database result is the official one. If the article's account of who won contradicts it, or the bout is missing from the article, or its section has no official result yet, set covered to false and leave the summary empty rather than guessing.

Return one entry for every listed fight id, and no others."""

SCHEMA = {
    'type': 'object',
    'properties': {
        'summaries': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'fight_id': {'type': 'string'},
                    'covered': {'type': 'boolean'},
                    'summary': {'type': 'string'},
                },
                'required': ['fight_id', 'covered', 'summary'],
                'additionalProperties': False,
            },
        },
    },
    'required': ['summaries'],
    'additionalProperties': False,
}

# ── Sherdog ───────────────────────────────────────────────────────────────────

def fetch_html(url):
    """GET a Sherdog page → text, or None on a non-200. Sherdog's news pages
    declare UTF-8 but some are served as Windows-1252, so decode the bytes
    ourselves instead of trusting the header."""
    r = s._fetch(url)
    time.sleep(DELAY)
    if r.status_code != 200:
        return None
    try:
        return r.content.decode('utf-8')
    except UnicodeDecodeError:
        return r.content.decode('cp1252', errors='replace')

def playbyplay_url(event_html):
    """The event page's own "VIEW PLAY-BY-PLAY" link. Matched on that exact
    label: the sidebar also links other cards' play-by-play articles."""
    soup = BeautifulSoup(event_html, 'html.parser')
    for a in soup.find_all('a', href=True):
        if re.fullmatch(r'view play-by-play', a.get_text(' ', strip=True), re.I):
            href = a['href']
            return s.SHERDOG_BASE + href if href.startswith('/') else href
    return None

def playbyplay_text(article_html):
    """`#playbyplay` → (plain text with markdown-style headings, bout count).
    Starts at the first bout heading, dropping the weigh-in list above it."""
    soup = BeautifulSoup(article_html, 'html.parser')
    body = soup.find(id='playbyplay')
    if body is None:
        return '', 0
    bouts = 0
    for h in body.find_all(['h2', 'h3', 'h4']):
        title = ' '.join(h.get_text(' ', strip=True).split())
        if h.name == 'h2':
            bouts += 1
        h.string = f'\x00{"#" * int(h.name[1])} {title}\x00'
    text = ' '.join(body.get_text(' ').split())
    text = re.sub(r' ?\x00 ?', '\n\n', text).strip()
    start = text.find('## ')
    return (text[start:] if start >= 0 else ''), bouts

# ── Model ─────────────────────────────────────────────────────────────────────

def _name_tokens(name):
    return [t for t in re.split(r'[^a-z]+', fold(name or '')) if len(t) >= 3]

def mentions_fighter(summary, fight):
    """Cheap guard against a summary attached to the wrong bout: it must name
    at least one of the two fighters. A word matches a name token when one
    starts with the other, so 'Alateng' passes for 'Alatengheili' but 'late'
    does not."""
    words = [w for w in _name_tokens(summary) if len(w) >= 4]
    for nm in (fight['fighter1_name'], fight['fighter2_name']):
        for tok in _name_tokens(nm):
            if any(tok.startswith(w) or w.startswith(tok) for w in words):
                return True
    return False

def _result_line(f):
    method = ' '.join(b for b in (f.get('details'), f.get('method')) if b and '|' not in b)
    if f.get('winner_name'):
        res = f'{f["winner_name"]} won by {method}' if method else f'{f["winner_name"]} won'
    else:
        res = f'{method or "draw or no contest"}, with no winner'
    bits = [res, f'round {f["round"]}' if f.get('round') else '', f.get('time') or '']
    return ', '.join(b for b in bits if b)

def summarise(client, event, fights, article):
    """One request for the whole card → ({fight_id: summary}, model id, usage)
    or (None, None, None) when the model declined or ran out of room."""
    listing = '\n'.join(
        f'- fight_id {f["id"]}: {f["fighter1_name"]} vs. {f["fighter2_name"]}'
        f' ({f.get("weight_class") or "weight class unknown"}). Official result: {_result_line(f)}.'
        for f in fights)
    prompt = (f'Event: {event["name"]} ({event["date"]})\n\n'
              f'Fights that need a summary:\n{listing}\n\n'
              f'<play_by_play>\n{article}\n</play_by_play>')
    resp = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        # If a safety classifier declines the request, the API retries it on
        # the fallback model inside the same call instead of returning nothing.
        betas=['server-side-fallback-2026-07-01'],
        fallbacks='default',
        system=SYSTEM,
        output_config={'effort': EFFORT, 'format': {'type': 'json_schema', 'schema': SCHEMA}},
        messages=[{'role': 'user', 'content': prompt}],
    )
    if resp.stop_reason in ('refusal', 'max_tokens'):
        print(f'    model stopped with {resp.stop_reason}; nothing written for this card')
        return None, None, None
    data = json.loads(next(b.text for b in resp.content if b.type == 'text'))
    by_id = {f['id']: f for f in fights}
    out = {}
    for row in data.get('summaries', []):
        fight = by_id.get(str(row.get('fight_id')))
        text = ' '.join((row.get('summary') or '').split())
        if not fight or not row.get('covered') or not text:
            continue
        if not mentions_fighter(text, fight):
            print(f'    dropped {fight["id"]}: summary names neither {fight["fighter1_name"]} nor {fight["fighter2_name"]}')
            continue
        out[fight['id']] = text
    return out, resp.model, resp.usage

# ── Runs ──────────────────────────────────────────────────────────────────────

FIGHT_COLS = 'id,event_id,fighter1_name,fighter2_name,winner_name,method,details,round,time,weight_class'

def fights_for(event_ids):
    """Fights with a recorded result, grouped by event id."""
    by_event = {}
    ids = list(event_ids)
    for i in range(0, len(ids), 100):
        chunk = ','.join(str(x) for x in ids[i:i + 100])
        for f in fetch_all('fight_search', FIGHT_COLS, f'&event_id=in.({chunk})&method=not.is.null'):
            by_event.setdefault(f['event_id'], []).append(f)
    return by_event

def process(client, event, fights, dry_run):
    """→ (summaries written, model requests made)."""
    label = f'{event["date"]} {event["name"]}'
    event_html = fetch_html(f'{s.SHERDOG_BASE}/events/x-{event["id"]}')
    if event_html is None:
        print(f'  {label}: event page not found')
        return 0, 0
    url = playbyplay_url(event_html)
    if not url:
        print(f'  {label}: no play-by-play on Sherdog')
        return 0, 0
    article_html = fetch_html(url)
    article, bouts = playbyplay_text(article_html or '')
    if not bouts or len(article) < MIN_ARTICLE_CHARS:
        print(f'  {label}: play-by-play has no rounds yet')
        return 0, 0
    if client is None:
        print(f'  {label}: {bouts} bouts in the play-by-play ({len(article):,} chars), '
              f'{len(fights)} fights need a summary — no API key, model not called')
        return 0, 0
    summaries, model, usage = summarise(client, event, fights, article)
    if summaries is None:
        return 0, 1
    print(f'  {label}: {len(summaries)}/{len(fights)} summaries from {bouts} bouts'
          f' ({usage.input_tokens:,} in / {usage.output_tokens:,} out tokens, {model})')
    if dry_run:
        names = {f['id']: f'{f["fighter1_name"]} vs. {f["fighter2_name"]}' for f in fights}
        for fid, text in summaries.items():
            print(f'    {names[fid]}: {text}')
        return len(summaries), 1
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    s.upsert('fight_summaries', [
        {'fight_id': fid, 'summary': text, 'source': 'sherdog', 'source_url': url,
         'model': model, 'generated_at': now}
        for fid, text in summaries.items()])
    return len(summaries), 1

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--event', type=int, help='one event by Sherdog id (ignores --org-ids, --days)')
    ap.add_argument('--all', action='store_true', help='every in-scope event, not just recent ones (backfill)')
    ap.add_argument('--days', type=int, default=14, help='nightly window: events from the last N days (default 14)')
    ap.add_argument('--org-ids', default=str(UFC_ORG_ID), help='comma-separated organization ids (default 2 = UFC)')
    ap.add_argument('--force', action='store_true', help='rewrite summaries that already exist')
    ap.add_argument('--limit', type=int, default=0, help='stop after N model requests (backfill in chunks)')
    ap.add_argument('--dry-run', action='store_true', help='print summaries, write nothing')
    args = ap.parse_args()
    if not s.SUPABASE_URL or not s.SUPABASE_KEY:
        sys.exit('Missing SUPABASE creds in .env')

    client = None
    if os.environ.get('ANTHROPIC_API_KEY'):
        import anthropic
        client = anthropic.Anthropic()
    elif not args.dry_run:
        print('ANTHROPIC_API_KEY is not set — skipping fight summaries.')
        return

    today = dt.date.today()
    if args.event:
        extra = f'&id=eq.{args.event}'
    else:
        extra = f'&organization_id=in.({args.org_ids})&date=lte.{today}'
        if not args.all:
            extra += f'&date=gte.{today - dt.timedelta(days=args.days)}'
    events = fetch_all('events', 'id,name,date', extra, order='date.desc')
    by_event = fights_for(e['id'] for e in events)
    have = set() if args.force else {
        r['fight_id'] for r in fetch_all('fight_summaries', 'fight_id', order='fight_id')}
    todo = [(e, [f for f in by_event.get(e['id'], []) if f['id'] not in have]) for e in events]
    todo = [(e, fs) for e, fs in todo if fs]
    print(f'{len(events)} events in scope, {len(todo)} with fights lacking a summary')

    n_rows = n_calls = 0
    for event, fights in todo:
        try:
            rows, calls = process(client, event, fights, args.dry_run)
        except requests.exceptions.RequestException as e:
            print(f'  {event["date"]} {event["name"]}: fetch failed ({e})')
            continue
        except Exception as e:
            # A model or network failure on one card must not fail the nightly;
            # an auth or billing problem will fail every card, so stop there.
            import anthropic
            if isinstance(e, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
                print(f'  Anthropic API rejected the key ({e}); stopping.')
                break
            if isinstance(e, (anthropic.APIError, json.JSONDecodeError, StopIteration)):
                print(f'  {event["date"]} {event["name"]}: model request failed ({type(e).__name__}: {e})')
                continue
            raise
        n_rows += rows
        n_calls += calls
        if args.limit and n_calls >= args.limit:
            print(f'--limit {args.limit} reached')
            break
    print(f'\n{n_calls} model requests, {n_rows} summaries'
          + (' (dry run — nothing written)' if args.dry_run else ''))

if __name__ == '__main__':
    main()
