#!/usr/bin/env python3
"""
weight_scraper.py — weight classes for fights Sherdog lists without one, into
`fight_weights`, from the bout tables on Wikipedia's event pages.

Why. Sherdog's event pages carry no weight class for most cards before 2012,
in any organisation, so `fights.weight_class` is blank for them. Wikipedia's
event articles hold a results table built from `{{MMAevent bout|<weight
class>|<fighter>|def.|<fighter>|<method>|<round>|<time>|<notes>}}`, one per
bout, for every UFC card.

PRIDE is configured but not run by default: Wikipedia's PRIDE tables leave
the weight class blank for about four bouts in five (checked 2026-11-23:
84 usable of 583), and the labels they do carry mix PRIDE's names with
today's. PRIDE needs another source.

Separate table on purpose: sherdog_scraper.py rewrites `fights.weight_class`
whenever it re-scrapes an event, which would blank a value stored there.
`fight_search` falls back to `fight_weights` when the fight's own is blank.

Name and limit. The class is stored under the name the bout was billed with,
plus the upper limit in pounds, because the same name meant different weights
in different places: a UFC "Middleweight" bout in 1999 was under 200 lb and a
PRIDE "Middleweight" bout was 205 lb. `weight_limit_lb` is null when the
class has no upper limit (Openweight, Super Heavyweight, the early UFC and
PRIDE Heavyweight). The app shows the limit beside the name only where it
differs from today's limit for that name, and groups by the limit. ERAS and
ufc_early() below are the only per-organisation knowledge; anything not
covered there gets today's limit for its name.

Matching. Pages are found from the organisation's "list of events" article
(every linked title that looks like an event), fetched 50 at a time. A page
can cover several cards, so bouts are matched to fights by the two fighters'
names across the whole organisation, not page to event; a pair that fought
more than once is told apart by round and time. No dates are needed.

  python weight_scraper.py                 # UFC fights with no weight class
  python weight_scraper.py --orgs PRIDE    # one organisation (events.organization)
  python weight_scraper.py --dry-run       # match and print, write nothing
  python weight_scraper.py --survey        # just count the labels Wikipedia uses, by year
"""
import re
import sys
import time
import argparse
import datetime as dt
from collections import Counter, defaultdict

import requests

import sherdog_scraper as s
from verdict_scraper import fetch_all, fold

API = 'https://en.wikipedia.org/w/api.php'
UA = {'User-Agent': 'fight-data-research/1.0 (python-requests)'}

# events.organization → (list article, regex an event page's title must match)
ORGS = {
    'UFC':   ('List of UFC events', r'^(UFC\b|The Ultimate Fighter\b|Ultimate Ultimate)| vs\. '),
    'PRIDE': ('List of Pride FC events', r'^(Pride\b|PRIDE\b|\d{4} in Pride)'),
}

# Today's limits (unified rules), by class name without the "Women's" prefix.
STANDARD = {
    'Atomweight': 105, 'Strawweight': 115, 'Flyweight': 125, 'Bantamweight': 135,
    'Featherweight': 145, 'Lightweight': 155, 'Welterweight': 170, 'Middleweight': 185,
    'Light Heavyweight': 205, 'Heavyweight': 265, 'Super Heavyweight': None, 'Openweight': None,
}

# (organization, first date, last date, billed name) → limit in lb (None = no
# upper limit). Dates are inclusive, ISO. PRIDE: Heavyweight over 93 kg,
# Middleweight 93 kg, Welterweight 83 kg, Lightweight 73 kg. The early UFC is
# handled by ufc_early() instead, because Wikipedia's labels for it are mixed.
ERAS = [
    ('PRIDE', '1997-01-01', '2007-12-31', 'Heavyweight', None),
    ('PRIDE', '1997-01-01', '2007-12-31', 'Middleweight', 205),
    ('PRIDE', '1997-01-01', '2007-12-31', 'Welterweight', 183),
    ('PRIDE', '1997-01-01', '2007-12-31', 'Lightweight', 161),
]

# The UFC's first weight classes. UFC 12 split the field into Heavyweight
# (200 lb and over) and Lightweight (under 200); UFC 14 renamed the lower one
# Middleweight; UFC 16 added Lightweight (under 170); UFC 26 added
# Bantamweight (under 155). UFC 31 adopted today's names and limits.
UFC_12, UFC_14, UFC_16, UFC_26, UFC_31 = '1997-02-07', '1997-07-27', '1998-03-13', '2000-06-09', '2001-05-04'

def ufc_early(label, date, page_labels):
    """Wikipedia's label for a UFC 12–30 bout → (billed name, limit).
    Its tables for these cards mix the names of the day with today's
    equivalents, sometimes on one page ("Welterweight" beside "Lightweight"
    at UFC 18, both the under-170 class), so the label is read as a weight
    band and the billed name is put back from the date. The one ambiguous
    label is "Lightweight" from UFC 26 on: the billed under-170 class, or
    today's name for the under-155 one. A page that also says "Bantamweight"
    is using the old names (so 170); otherwise one that says "Welterweight"
    is using today's (so 155)."""
    if label in ('Heavyweight', 'Super Heavyweight', 'Openweight') or 'Catchweight' in label:
        return label, None
    if date < UFC_14:
        return 'Lightweight', 200
    if label in ('Light Heavyweight', 'Middleweight') or date < UFC_16:
        return 'Middleweight', 200
    if label == 'Bantamweight':
        return 'Bantamweight', 155
    if label == 'Lightweight' and date >= UFC_26 and 'Welterweight' in page_labels and 'Bantamweight' not in page_labels:
        return 'Bantamweight', 155
    if label in ('Lightweight', 'Welterweight'):
        return 'Lightweight', 170
    return label, None                                 # anything unexpected: keep the label, no limit

UFC_FIRST_CLASSES = UFC_12          # every UFC bout before this was open weight

# ── Wikipedia ─────────────────────────────────────────────────────────────────

def _api(params):
    for attempt in range(6):
        r = requests.get(API, params={**params, 'format': 'json', 'formatversion': 2}, headers=UA, timeout=40)
        if r.status_code == 200:
            time.sleep(1.0)
            return r.json()
        time.sleep(float(r.headers.get('Retry-After') or 0) or 10 + attempt * 20)
    r.raise_for_status()

def event_titles(list_page, pattern):
    """Titles linked from the organisation's list article that look like events."""
    titles, cont = set(), {}
    while True:
        j = _api({'action': 'query', 'titles': list_page, 'prop': 'links', 'pllimit': 'max',
                  'plnamespace': 0, 'redirects': 1, **cont})
        for page in j['query']['pages']:
            for l in page.get('links', []):
                if re.search(pattern, l['title']):
                    titles.add(l['title'])
        if 'continue' not in j:
            return sorted(titles)
        cont = j['continue']

def page_texts(titles, cache=None):
    """_page_texts with an optional JSON file cache (--cache), for repeat runs."""
    import json, os
    if cache and os.path.exists(cache):
        have = json.load(open(cache, encoding='utf-8'))
        if set(titles) <= set(have.get('titles', [])):
            return have['pages']
    pages = _page_texts(titles)
    if cache:
        json.dump({'titles': titles, 'pages': pages}, open(cache, 'w', encoding='utf-8'))
    return pages

def _page_texts(titles):
    """{resolved title: wikitext}; redirects followed, so several requested
    titles can land on one page (PRIDE's cards live on "<year> in Pride FC")."""
    out = {}
    for i in range(0, len(titles), 50):
        j = _api({'action': 'query', 'titles': '|'.join(titles[i:i + 50]), 'prop': 'revisions',
                  'rvprop': 'content', 'rvslots': 'main', 'redirects': 1})
        for page in j['query'].get('pages', []):
            revs = page.get('revisions')
            if revs:
                out[page['title']] = revs[0]['slots']['main']['content']
    return out

def _templates(text, name):
    """Each `{{name|...}}` in text → list of top-level parameters."""
    out, pos, start = [], 0, '{{' + name
    while True:
        i = text.find(start, pos)
        if i < 0:
            return out
        depth, j, params, cur = 0, i, [], []
        while j < len(text):
            two = text[j:j + 2]
            if two in ('{{', '[['):
                depth += 1; cur.append(two); j += 2
            elif two in ('}}', ']]'):
                depth -= 1
                if depth == 0:
                    params.append(''.join(cur)); j += 2
                    break
                cur.append(two); j += 2
            elif text[j] == '|' and depth == 1:
                params.append(''.join(cur)); cur = []; j += 1
            else:
                cur.append(text[j]); j += 1
        out.append([p.strip() for p in params[1:]])   # params[0] is the template name
        pos = j

def _plain(wikitext):
    """Wiki markup → display text: drop refs, comments and templates, keep a
    link's label."""
    t = re.sub(r'<ref[^>]*/>|<ref[^>]*>.*?</ref>|<!--.*?-->', '', wikitext, flags=re.S)
    for _ in range(3):
        t = re.sub(r'\{\{[^{}]*\}\}', '', t)
    t = re.sub(r'\[\[(?:[^\]|]*\|)?([^\]]*)\]\]', r'\1', t)
    t = re.sub(r"'{2,}|<[^>]+>", '', t)
    return ' '.join(t.split())

def _fighter(wikitext):
    return re.sub(r'\s*\((?:c|ic|C|IC)\)\s*', ' ', _plain(wikitext)).strip()

def _link(wikitext):
    """Title of the first article link in a table cell, or None."""
    m = re.search(r'\[\[([^\]|#]+)', re.sub(r'\{\{[^{}]*\}\}', '', wikitext))
    return m.group(1).strip() if m else None

def bouts(text):
    """→ [{label, a, b, round, time, a_link, b_link, notes}] for every bout
    template on a page."""
    out = []
    for p in _templates(text, 'MMAevent bout'):
        if len(p) < 4:
            continue
        p += [''] * 8
        out.append({'label': _plain(p[0]), 'a': _fighter(p[1]), 'b': _fighter(p[3]),
                    'round': _plain(p[5]), 'time': _plain(p[6]),
                    'a_link': _link(p[1]), 'b_link': _link(p[3]), 'notes': _plain(p[7])})
    return out

# ── Labels and limits ─────────────────────────────────────────────────────────

def normalise(label):
    """Wikipedia's label → the DB's spelling, or None when it names no class
    ("N/A", blank). Catchweights follow Sherdog's "160lb Catchweight"."""
    t = ' '.join((label or '').replace('’', "'").split())
    if not t or t.lower() in ('n/a', 'na', '-', '—', '–', 'tba', 'superfight', 'exhibition'):
        return None
    women = bool(re.match(r"women'?s\b", t, re.I))
    core = re.sub(r"^women'?s\s+", '', t, flags=re.I)
    if re.search(r'catch\s*weight', core, re.I):
        m = re.search(r'(\d{3}(?:\.\d)?)\s*(?:lbs?|pounds)', core, re.I)
        kg = re.search(r'(\d{2,3}(?:\.\d)?)\s*kg', core, re.I)
        lb = m.group(1) if m else (str(round(float(kg.group(1)) * 2.20462)) if kg else '')
        core = f'{lb.split(".")[0]}lb Catchweight' if lb else 'Catchweight'
    else:
        core = re.sub(r'\s*\(.*?\)\s*', ' ', core).strip()      # "Lightweight (155 lb)"
        core = {'open weight': 'Openweight', 'absolute': 'Openweight', 'no weight limit': 'Openweight',
                'superheavyweight': 'Super Heavyweight'}.get(core.lower(), core.title())
    return f"Women's {core}" if women else core

def limit_for(org, date, name):
    """Upper limit in lb for a billed class, or None when it has none / is
    unknown (a catchweight carries its number in the name)."""
    core = re.sub(r"^Women's ", '', name)
    for o, lo, hi, n, lim in ERAS:
        if o == org and n == core and lo <= date <= hi:
            return lim
    m = re.match(r'(\d+)lb Catchweight', core)
    return int(m.group(1)) if m else STANDARD.get(core)

# ── Matching ──────────────────────────────────────────────────────────────────

# Sherdog's legal name → the ring name Wikipedia's tables use.
ALIASES = {'kevinferguson': 'kimboslice', 'mirkofilipovic': 'mirkocrocop', 'davidabbott': 'tankabbott'}

def _key(name):
    k = re.sub(r'[^a-z]', '', fold(name or ''))
    return ALIASES.get(k, k)

def _surname(name):
    toks = [t for t in re.split(r'[^a-z]+', fold(name or '')) if t and t not in ('jr', 'sr', 'ii', 'iii')]
    return toks[-1] if toks else ''

def _clock(t):
    m = re.search(r'(\d+):(\d\d)', t or '')
    return f'{int(m.group(1))}:{m.group(2)}' if m else ''

def _same_finish(fight, bout):
    r = re.search(r'\d+', bout['round'] or '')
    return bool(r) and str(fight.get('round')) == r.group(0) and _clock(fight.get('time')) == _clock(bout['time']) != ''

def match(fights, wiki_bouts):
    """→ ({fight id: (bout, url)}, stats). Full names first; then surnames,
    which needs the round and time to agree as well. Among several bouts for
    one pair (rematches) the one with the same round and time wins."""
    by_pair, by_sur = defaultdict(list), defaultdict(list)
    for b, url in wiki_bouts:
        by_pair[frozenset((_key(b['a']), _key(b['b'])))].append((b, url))
        by_sur[frozenset((_surname(b['a']), _surname(b['b'])))].append((b, url))
    out, stats = {}, Counter()
    for f in fights:
        full = by_pair.get(frozenset((_key(f['fighter1_name']), _key(f['fighter2_name']))), [])
        exact = [c for c in full if _same_finish(f, c[0])]
        if exact or len(full) == 1:
            out[f['id']] = (exact or full)[0]; stats['by name'] += 1
            continue
        if full:
            labels = {normalise(c[0]['label']) for c in full}
            if len(labels) == 1:                      # rematches, same class every time
                out[f['id']] = full[0]; stats['by name'] += 1
            else:
                stats['ambiguous rematch'] += 1
            continue
        sur = [c for c in by_sur.get(frozenset((_surname(f['fighter1_name']), _surname(f['fighter2_name']))), [])
               if _same_finish(f, c[0])]
        if len(sur) == 1:
            out[f['id']] = sur[0]; stats['by surname + finish'] += 1
        else:
            stats['unmatched'] += 1
    return out, stats

# ── Run ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--orgs', default='UFC', help='comma-separated events.organization values (default UFC)')
    ap.add_argument('--dry-run', action='store_true', help='match and print, write nothing')
    ap.add_argument('--cache', help='path prefix for a local cache of the fetched pages')
    ap.add_argument('--survey', action='store_true', help="count Wikipedia's labels by year for the matched fights")
    args = ap.parse_args()
    if not s.SUPABASE_URL or not s.SUPABASE_KEY:
        sys.exit('Missing SUPABASE creds in .env')

    now = dt.datetime.now(dt.timezone.utc).isoformat()
    for org in [o.strip() for o in args.orgs.split(',') if o.strip()]:
        if org not in ORGS:
            print(f'{org}: no Wikipedia list configured (ORGS)'); continue
        list_page, pattern = ORGS[org]
        fights = [f for f in fetch_all(
            'fight_search', 'id,fighter1_name,fighter2_name,round,time,event_date,event_name,weight_class',
            f'&event_organization=eq.{org}&event_date=lte.{dt.date.today()}', order='id')
            if not (f.get('weight_class') or '').strip()]
        print(f'{org}: {len(fights)} fights with no weight class')
        if not fights:
            continue
        titles = event_titles(list_page, pattern)
        pages = page_texts(titles, args.cache and f'{args.cache}.{org}.json')
        wiki = [(b, 'https://en.wikipedia.org/wiki/' + title.replace(' ', '_'))
                for title, text in pages.items() for b in bouts(text)]
        print(f'  {len(titles)} linked titles → {len(pages)} pages, {len(wiki)} bouts')
        matched, stats = match(fights, wiki)

        page_labels = defaultdict(set)
        for b, url in wiki:
            page_labels[url].add(normalise(b['label']))
        rows, labels, skipped = [], Counter(), Counter()
        for f in fights:
            date = f['event_date']
            if org == 'UFC' and date < UFC_FIRST_CLASSES:
                name, url = 'Openweight', None       # no classes existed yet, whatever the page says
            elif f['id'] in matched:
                bout, url = matched[f['id']]
                name = normalise(bout['label'])
                if not name:
                    skipped[bout['label'] or '(blank)'] += 1
                    continue
            else:
                continue
            if org == 'UFC' and UFC_12 <= date < UFC_31:
                name, limit = ufc_early(name, date, page_labels[url])
            else:
                limit = limit_for(org, date, name)
            labels[(date[:4], f'{name} [{limit}]')] += 1
            rows.append({'fight_id': f['id'], 'weight_class': name, 'weight_limit_lb': limit,
                         'source': 'wikipedia' if url else 'rule', 'source_url': url, 'updated_at': now})
        print(f'  matched: {dict(stats)}; {len(rows)} rows; labels with no class skipped: {dict(skipped)}')
        if args.survey or args.dry_run:
            by_year = defaultdict(list)
            for (yr, name), n in sorted(labels.items()):
                by_year[yr].append(f'{name} {n}')
            for yr, bits in by_year.items():
                print(f'    {yr}: ' + ', '.join(bits))
            missing = [f for f in fights if f['id'] not in {r["fight_id"] for r in rows}]
            for f in missing[:25]:
                print(f'    no weight: {f["event_date"]} {f["fighter1_name"]} vs. {f["fighter2_name"]} ({f["event_name"]})')
            if len(missing) > 25:
                print(f'    … and {len(missing) - 25} more')
        if not args.dry_run and not args.survey and rows:
            for i in range(0, len(rows), 500):
                s.upsert('fight_weights', rows[i:i + 500])
            print(f'  wrote {len(rows)} rows to fight_weights')

if __name__ == '__main__':
    main()
