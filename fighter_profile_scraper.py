#!/usr/bin/env python3
"""
fighter_profile_scraper.py — fighter photo + bio from Sherdog FIGHTER pages.

sherdog_scraper.py only reads EVENT pages, which carry a fighter's name and id
and nothing else. The photo and bio live on the fighter's own page, so this
crawls /fighter/<id> and fills fighters.image_url / nationality / date_of_birth
/ height / association.

No sherdog_url is stored: fighters.id IS the Sherdog fighter id, and
https://www.sherdog.com/fighter/<id> redirects to the canonical slug URL, so the
app builds the link from the id.

Resumable via fighters.profile_scraped_at (null = never crawled) — a DB column
rather than a tracker file, because a 38k-id text file committed by the nightly
workflow would be pure churn. Every crawled fighter is stamped, including ones
with no photo (Sherdog serves a fighter_large_default.jpg placeholder, which is
stored as a null image_url so the app can fall back to initials).

A backlog is crawled busiest-fighter-first, so the names most likely to be
looked up get their photo early rather than after every regional debutant.

  python fighter_profile_scraper.py                 # every uncrawled fighter
  python fighter_profile_scraper.py --limit 500     # chunked (busiest 500 first)
  python fighter_profile_scraper.py --refresh 365   # also re-crawl stale profiles
  python fighter_profile_scraper.py --ids 27944,461 # targeted re-crawl
  python fighter_profile_scraper.py --limit 5 --dry-run
"""
import re
import sys
import json
import time
import argparse
import datetime as dt

import requests
from bs4 import BeautifulSoup

import sherdog_scraper as s   # reuse _fetch, _headers, upsert, SUPABASE_*, env load

# Sherdog's "this fighter has no photo" placeholder — store null instead.
PLACEHOLDER_RE = re.compile(r'fighter_large_default|_no_pic|blank', re.I)
FIELDS = ('image_url', 'nationality', 'date_of_birth', 'height', 'association')
BATCH = 100          # fighters per Supabase upsert
SLEEP = 0.3          # seconds between fighter pages (matches amateur_scraper.py)


def fetch_all(table, select, extra=''):
    """Page through a Supabase table."""
    PAGE, off, out = 1000, 0, []
    while True:
        r = requests.get(f'{s.SUPABASE_URL}/rest/v1/{table}?select={select}'
                         f'{extra}&order=id.desc&limit={PAGE}&offset={off}',
                         headers=s._headers())
        r.raise_for_status()
        batch = r.json()
        out += batch
        if len(batch) < PAGE:
            return out
        off += PAGE


def fight_counts():
    """fighter_id -> number of fights in the DB, for crawl ordering."""
    counts = {}
    for f in fetch_all('fights', 'id,fighter1_id,fighter2_id'):
        for fid in (f['fighter1_id'], f['fighter2_id']):
            if fid:
                counts[fid] = counts.get(fid, 0) + 1
    return counts


def _text(node):
    return re.sub(r'\s+', ' ', node.get_text(' ', strip=True)).strip() if node else None


def parse_date(raw):
    """'Jul 19, 1987' -> '1987-07-19'. Sherdog shows 'N/A' when unknown."""
    if not raw or raw.strip().upper() in ('N/A', '-'):
        return None
    try:
        return dt.datetime.strptime(raw.strip(), '%b %d, %Y').date().isoformat()
    except ValueError:
        return None


def parse_profile(html):
    """Profile fields off a Sherdog fighter page. Missing fields come back None."""
    soup = BeautifulSoup(html, 'html.parser')

    img = (soup.select_one('img.profile-image[itemprop="image"]')
           or soup.select_one('img.profile-image'))
    src = img.get('src') if img else None
    image_url = None
    if src and not PLACEHOLDER_RE.search(src):
        image_url = src if src.startswith('http') else f'{s.SHERDOG_BASE}{src}'

    # Height renders as: <b itemprop="height">6'4"</b><em>/</em>193.04 cm
    height = _text(soup.select_one('b[itemprop="height"]'))
    if height:
        height = height.rstrip(' /') or None

    return {
        'image_url':     image_url,
        'nationality':   _text(soup.select_one('[itemprop="nationality"]')),
        'date_of_birth': parse_date(_text(soup.select_one('[itemprop="birthDate"]'))),
        'height':        height,
        'association':   _text(soup.select_one(
            '.association-class a.association [itemprop="name"]')),
    }


def scrape_fighter(fighter_id):
    """Profile dict for one fighter, or None if the page couldn't be read.

    An unknown id doesn't 404 — Sherdog serves a stub at /fighter/index?id=N
    with no profile markup. That's still a definitive answer, so it's stamped
    (with empty fields) rather than retried every run.
    """
    resp = s._fetch(f'{s.SHERDOG_BASE}/fighter/{fighter_id}')
    if resp.status_code != 200:
        return None
    if 'fighter/index?id=' in resp.url:
        return {k: None for k in FIELDS}
    return parse_profile(resp.text)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=None,
                    help='max fighters to crawl this run')
    ap.add_argument('--refresh', type=int, default=None, metavar='DAYS',
                    help='also re-crawl profiles last scraped over DAYS ago')
    ap.add_argument('--ids', type=str, default=None,
                    help='comma-separated fighter ids to (re-)crawl')
    ap.add_argument('--dry-run', action='store_true',
                    help='print what would be written, push nothing')
    args = ap.parse_args()
    if not s.SUPABASE_URL or not s.SUPABASE_KEY:
        sys.exit('Missing SUPABASE creds in .env')

    print('Loading fighters...')
    if args.ids:
        inlist = ','.join(x.strip() for x in args.ids.split(',') if x.strip().isdigit())
        todo = fetch_all('fighters', 'id,name', f'&id=in.({inlist})')
    else:
        todo = fetch_all('fighters', 'id,name', '&profile_scraped_at=is.null')
        if args.refresh:
            cutoff = (dt.datetime.now(dt.timezone.utc)
                      - dt.timedelta(days=args.refresh)).isoformat()
            todo += fetch_all('fighters', 'id,name', f'&profile_scraped_at=lt.{cutoff}')

    # Busiest fighters first, so the names most likely to be looked up get their
    # photo early in a long backfill. Counting fights costs a full scan of the
    # fights table, so only bother when the queue is big enough to care — a
    # nightly run is a handful of new fighters and takes the id order as-is.
    if len(todo) > 1000:
        print('  ordering by fight count...')
        counts = fight_counts()
        todo.sort(key=lambda f: counts.get(f['id'], 0), reverse=True)

    if args.limit:
        todo = todo[:args.limit]
    print(f'  {len(todo)} fighters to crawl\n')

    pending, done, with_photo, missing = [], 0, 0, 0

    def flush():
        if pending and not args.dry_run:
            s.upsert('fighters', pending)
        pending.clear()

    for f in todo:
        try:
            profile = scrape_fighter(f['id'])
        except Exception as e:
            print(f'  [WARN] fighter {f["id"]} ({f["name"]}): {e}')
            continue          # unstamped -> retried next run
        if profile is None:
            print(f'  [WARN] fighter {f["id"]} ({f["name"]}): non-200')
            continue

        if profile['image_url']:
            with_photo += 1
        else:
            missing += 1
        # name is sent so the upsert's INSERT arm can't trip fighters.name
        # NOT NULL — the row always exists, so the ON CONFLICT arm is what runs.
        pending.append({'id': f['id'], 'name': f['name'], **profile,
                        'profile_scraped_at': dt.datetime.now(dt.timezone.utc).isoformat()})
        done += 1

        if args.dry_run:
            print(f'  {f["id"]:>7}  {f["name"][:28]:<28} {json.dumps(profile)}')
        if len(pending) >= BATCH:
            flush()             # persist before moving on — the run is resumable
            print(f'  ...{done}/{len(todo)}  ({with_photo} photos, {missing} without)')
        time.sleep(SLEEP)

    flush()
    verb = 'would update' if args.dry_run else 'updated'
    print(f'\nDone — {verb} {done} fighters ({with_photo} with a photo, '
          f'{missing} without).')


if __name__ == '__main__':
    main()
