#!/usr/bin/env python3
"""
Sherdog Fight Scraper -> Supabase
Scrapes non-UFC promotions from sherdog.com and pushes results into Supabase.
Currently covers PFL and MVP. Mirrors start.py (the UFC/ufcstats scraper).

Setup:
  pip install requests beautifulsoup4
  Fill .env with SUPABASE_URL and SUPABASE_KEY
  Run: python sherdog_scraper.py
"""

import os
import re
import uuid
import time
import json
import requests
from datetime import datetime
from bs4 import BeautifulSoup

# Load .env
_env_path = os.path.join(os.path.dirname(__file__), '.env')
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith('#') and '=' in _line:
                _k, _v = _line.split('=', 1)
                os.environ.setdefault(_k.strip(), _v.strip())

# ── Config ────────────────────────────────────────────────────────────────────

SUPABASE_URL = os.environ.get('SUPABASE_URL', '').rstrip('/')
SUPABASE_KEY = os.environ.get('SUPABASE_KEY', '')

COMPLETED_EVENTS_FILE = 'sherdog_completed_events.txt'
SHERDOG_BASE = 'https://www.sherdog.com'

USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36')

# Fixed namespace for deterministic UUIDs — must match start.py
_NS = uuid.UUID('6ba7b810-9dad-11d1-80b4-00c04fd430c8')

# Each org: Sherdog org page, the slug keywords that identify its own events
# (the org page also links 'related events' from other promotions), the
# name-prefix cleanup, and the fight-id prefix (do NOT change once data exists).
# 'active': scraped by the daily run. Defunct orgs are inactive — scraped once
# with --all, then skipped (no new events ever).
SHERDOG_ORGS = [
    {
        # UFC — the bulk of the DB. Sherdog files UFC's own events AND the
        # Road to UFC developmental series under the same org page (oid=2);
        # exclude the RTUFC slugs here so the two entries don't double-count.
        # This makes Sherdog the source of truth for UFC too (ufcstats retired).
        'organization': 'UFC',
        'org_url':      f'{SHERDOG_BASE}/organizations/Ultimate-Fighting-Championship-UFC-2',
        'slug_keys':    ('ufc-',),
        'exclude_slug_keys': ('road-to-ufc',),
        'name_clean':   (r'^$', ''),   # Sherdog UFC names already clean ("UFC 300 - ...")
        'fight_prefix': 'ufcfight',
        'active':       True,
    },
    {
        'organization': 'PFL',
        'org_url':      f'{SHERDOG_BASE}/organizations/Professional-Fighters-League-12241',
        'slug_keys':    ('professional-fighters-league', 'pfl-'),
        'name_clean':   (r'^Professional Fighters League\s*-\s*', ''),
        'fight_prefix': 'pflfight',
        'active':       True,
    },
    {
        'organization': 'MVP',
        'org_url':      f'{SHERDOG_BASE}/organizations/Most-Valuable-Promotions-19337',
        'slug_keys':    ('most-valuable-promotions',),
        'name_clean':   (r'^Most Valuable Promotions\s*-\s*', 'MVP: '),
        'fight_prefix': 'mvpfight',
        'active':       True,
    },
    {
        # Defunct — folded into PFL. One-time scrape.
        'organization': 'Bellator',
        'org_url':      f'{SHERDOG_BASE}/organizations/Bellator-MMA-1960',
        'slug_keys':    ('bellator',),
        'name_clean':   (r'^Bellator MMA\s*-\s*', ''),
        'fight_prefix': 'bellatorfight',
        'active':       False,
    },
    {
        # Defunct — rebranded to PFL. One-time scrape.
        'organization': 'WSOF',
        'org_url':      f'{SHERDOG_BASE}/organizations/World-Series-of-Fighting-5449',
        'slug_keys':    ('wsof',),
        'name_clean':   (r'^World Series of Fighting\s*-\s*', ''),
        'fight_prefix': 'wsoffight',
        'active':       False,
    },
    {
        # Defunct — ceased 2007 after the Zuffa acquisition. Lives on Fight Pass.
        'organization': 'PRIDE',
        'org_url':      f'{SHERDOG_BASE}/organizations/Pride-Fighting-Championships-3',
        'slug_keys':    ('pride-',),
        'name_clean':   (r'^$', ''),   # no-op: names already clean (e.g. "Pride 10 - ...")
        'fight_prefix': 'pridefight',
        'active':       False,
    },
    {
        # Defunct — bought by Zuffa, last event 2013. Lives on Fight Pass.
        'organization': 'Strikeforce',
        'org_url':      f'{SHERDOG_BASE}/organizations/Strikeforce-716',
        'slug_keys':    ('strikeforce',),
        'name_clean':   (r'^$', ''),   # no-op: names already start with "Strikeforce - "
        'fight_prefix': 'sffight',
        'active':       False,
    },
    {
        # Defunct — merged into UFC 2010. Lives on Fight Pass.
        'organization': 'WEC',
        'org_url':      f'{SHERDOG_BASE}/organizations/World-Extreme-Cagefighting-48',
        'slug_keys':    ('wec-',),
        'name_clean':   (r'^$', ''),   # no-op: names already clean (e.g. "WEC 1 - ...")
        'fight_prefix': 'wecfight',
        'active':       False,
    },
    {
        # UFC developmental series. Sherdog files RTUFC events under the
        # UFC org page (oid=2) — we filter by slug pattern to pick them out.
        # Active: a new season runs annually.
        'organization': 'Road to UFC',
        'org_url':      f'{SHERDOG_BASE}/organizations/Ultimate-Fighting-Championship-UFC-2',
        'slug_keys':    ('ufc-road-to-ufc-',),
        'name_clean':   (r'^UFC\s*-\s*', ''),   # strip "UFC - " prefix
        'fight_prefix': 'rtufight',
        'active':       True,
    },
    {
        # UFC developmental series (Dana White's Contender Series). Its own
        # Sherdog org (12411), ~89 events. Names come as "Dana White's Contender
        # Series - Contender Series 2024: Week 1" — strip the redundant prefix.
        # Active: a new season runs annually.
        'organization': 'DWCS',
        'org_url':      f'{SHERDOG_BASE}/organizations/Dana-Whites-Contender-Series-12411',
        # 'contender-series' matches both the modern slug and the 2017-18
        # "Dana-Whites-Tuesday-Night-Contender-Series-..." era (incl. Brazil).
        'slug_keys':    ('contender-series',),
        'name_clean':   (r"^Dana White's (?:Tuesday Night )?Contender Series\s*-\s*", ''),
        'fight_prefix': 'dwcsfight',
        'active':       True,
    },
    {
        # Defunct — only 2 events ever (Banned 2008, Day of Reckoning 2009)
        # but legendary cards (Fedor vs Sylvia, Fedor vs Arlovski).
        'organization': 'Affliction',
        'org_url':      f'{SHERDOG_BASE}/organizations/Affliction-1459',
        'slug_keys':    ('affliction-',),
        'name_clean':   (r'^$', ''),   # names already start with "Affliction"
        'fight_prefix': 'afflictionfight',
        'active':       False,
    },
    {
        # Defunct — classic Japanese MMA, multiple eras. Fight Pass has 19+
        # standalone fight VODs in "X vs Y Vale Tudo Japan ..." format.
        'organization': 'Vale Tudo Japan',
        'org_url':      f'{SHERDOG_BASE}/organizations/Vale-Tudo-Japan-12',
        'slug_keys':    ('vale-tudo-japan-',),
        'name_clean':   (r'^$', ''),   # already prefixed "Vale Tudo Japan"
        'fight_prefix': 'vtjfight',
        'active':       False,
    },
    {
        # Active UK/European feeder. McGregor, Edwards, Pimblett all came
        # from here. Sherdog event slugs are "CW-NNN-Cage-Warriors-NNN-..."
        # (CW abbrev). Lots of events — paginates back to Cage Warriors 1.
        # Names come back like "CW 207 - Cage Warriors 207: Manchester" —
        # strip the redundant "CW NNN - " prefix.
        'organization': 'Cage Warriors',
        'org_url':      f'{SHERDOG_BASE}/organizations/Cage-Warriors-186',
        'slug_keys':    ('cw-', 'cage-warriors-'),
        'name_clean':   (r'^CW\s+\d+\s*-\s*', ''),
        'fight_prefix': 'cwfight',
        'active':       True,
    },
    {
        # Women's MMA promotion, ~50 events. Founded 2012 — major pre-UFC
        # outlet for women's MMA before the UFC opened up.
        'organization': 'Invicta FC',
        'org_url':      f'{SHERDOG_BASE}/organizations/Invicta-Fighting-Championships-4469',
        'slug_keys':    ('invicta-',),
        'name_clean':   (r'^$', ''),   # already prefixed "Invicta FC"
        'fight_prefix': 'invictafight',
        'active':       True,
    },
    {
        # Active US feeder (Legacy Fighting Alliance). Heir to RFA + Legacy.
        'organization': 'LFA',
        'org_url':      f'{SHERDOG_BASE}/organizations/Legacy-Fighting-Alliance-LFA-11339',
        'slug_keys':    ('lfa-',),
        'name_clean':   (r'^$', ''),   # already prefixed "LFA"
        'fight_prefix': 'lfafight',
        'active':       True,
    },
    {
        # Active US east-coast feeder (Cage Fury Fighting Championships).
        # Sherdog names duplicate the abbrev: "CFFC 100 - Cage Fury Fighting
        # Championships 100" — strip the redundant trailing copy.
        'organization': 'CFFC',
        'org_url':      f'{SHERDOG_BASE}/organizations/Cage-Fury-Fighting-Championships-813',
        'slug_keys':    ('cffc-',),
        'name_clean':   (r'\s*-\s*Cage\s+Fury\s+Fighting\s+Championships(?:\s+\d+)?', ''),
        'fight_prefix': 'cffcfight',
        'active':       True,
    },
    {
        # Japanese Pancrase (1993-present). 60+ year history; lots of events.
        # 'pancrase-' slug_key also picks up Deep & Pancrase joint events
        # (when we add DEEP later, those need exclusion to avoid dup fights).
        'organization': 'Pancrase',
        'org_url':      f'{SHERDOG_BASE}/organizations/Pancrase-19',
        'slug_keys':    ('pancrase-',),
        'name_clean':   (r'^$', ''),   # already prefixed
        'fight_prefix': 'pancrasefight',
        'active':       True,
    },
    {
        # Defunct — folded 2008 (Kimbo Slice, Gina Carano era). ~30 events.
        # Fight Pass has per-fight VODs. Slugs are "EliteXC-...".
        'organization': 'EliteXC',
        'org_url':      f'{SHERDOG_BASE}/organizations/EliteXC-937',
        'slug_keys':    ('elitexc',),
        'name_clean':   (r'^$', ''),   # names already "EliteXC - ..."
        'fight_prefix': 'elitexcfight',
        'active':       False,
    },
    {
        # King of the Cage (1998-present). Long-running US regional, 100s of
        # events. Sherdog event slugs are "KOTC-...". Per-fight VODs on FP.
        'organization': 'King of the Cage',
        'org_url':      f'{SHERDOG_BASE}/organizations/King-of-the-Cage-1',
        'slug_keys':    ('kotc-',),
        'name_clean':   (r'^$', ''),   # names already "KOTC - ..."
        'fight_prefix': 'kotcfight',
        'active':       True,
    },
    {
        # Shooto (Japan, 1989-present). 100s of events; slugs "Shooto-...".
        # Shooto Brazil is a SEPARATE Sherdog org (Shooto-Brazil-13715, its
        # own entry below) — this org page holds no Brazil events.
        # Kanji names romanize inconsistently — expect some alias work.
        'organization': 'Shooto',
        'org_url':      f'{SHERDOG_BASE}/organizations/Shooto-21',
        'slug_keys':    ('shooto',),
        'name_clean':   (r'^$', ''),   # names already "Shooto - ..."
        'fight_prefix': 'shootofight',
        'active':       True,
    },
    {
        # RIZIN Fighting Federation (Japan, 2015-present). Records-only on
        # Sherdog — Fight Pass has zero RIZIN content (confirmed via vesper).
        # Videos come via the official RIZIN YouTube channel (see youtube_scraper.py).
        # Strip the redundant ' - Rizin Fighting Federation N' name suffix.
        'organization': 'RIZIN',
        'org_url':      f'{SHERDOG_BASE}/organizations/Rizin-Fighting-Federation-10333',
        'slug_keys':    ('rizin-',),
        'name_clean':   (r'\s*-\s*Rizin\s+Fighting\s+Federation(\s+\d+)?', ''),
        'fight_prefix': 'rizinfight',
        'active':       True,
    },
    {
        # Fury Fighting Championship (Houston, TX; 2017-present). ~155 events;
        # active US feeder with Fight Pass per-fight VODs. DISTINCT from CFFC
        # (Cage Fury FC) — Fury FC slugs are "Fury-FC-NNN-...", CFFC's are
        # "CFFC-..."; 'fury-fc-' does not match CFFC's slugs.
        'organization': 'Fury FC',
        'org_url':      f'{SHERDOG_BASE}/organizations/Fury-Fighting-Championship-7617',
        'slug_keys':    ('fury-fc-',),
        'name_clean':   (r'\s*-\s*Fury\s+Fighting\s+Championship\s+\d+', ''),
        'fight_prefix': 'furyfcfight',
        'active':       True,
    },
    {
        # Titan FC (Kansas/US, 2006-present). ~83 events; on Fight Pass since
        # 2014. Sherdog org 740 (NOT the Japan/Argentina/Combat namesakes).
        # Slugs are "Titan-FC-NNN-..."; 'titan-fc-' avoids the other Titan orgs.
        'organization': 'Titan FC',
        'org_url':      f'{SHERDOG_BASE}/organizations/Titan-Fighting-Championship-740',
        'slug_keys':    ('titan-fc-',),
        'name_clean':   (r'^$', ''),   # names already "Titan FC NN - ..."
        'fight_prefix': 'titanfcfight',
        'active':       True,
    },
    # ── 2026-07 Fight Pass expansion wave ─────────────────────────────────────
    {
        # Abu Dhabi's flagship promotion (2019-present), ~76 events. Heavy
        # Fight Pass per-fight coverage ("X vs Y UAE Warriors 71").
        'organization': 'UAE Warriors',
        'org_url':      f'{SHERDOG_BASE}/organizations/UAE-Warriors-5307',
        'slug_keys':    ('uae-warriors-',),
        'name_clean':   (r'^$', ''),   # names already "UAE Warriors 72 - ..."
        'fight_prefix': 'uaewfight',
        'active':       True,
    },
    {
        # Australia's top promotion (~107 events). FP: "X vs Y Eternal MMA 103".
        'organization': 'Eternal MMA',
        'org_url':      f'{SHERDOG_BASE}/organizations/Eternal-MMA-5231',
        'slug_keys':    ('eternal-mma-',),
        'name_clean':   (r'^$', ''),   # names already "Eternal MMA 107 - ..."
        'fight_prefix': 'eternalfight',
        'active':       True,
    },
    {
        # French promotion (2019-present), ~43 events. Sherdog names events
        # "Ares 42 - ..." (no FC); Fight Pass titles say "Ares FC 42".
        # Exclude the unrelated "Ares Chaos MMA" org's slugs.
        'organization': 'Ares FC',
        'org_url':      f'{SHERDOG_BASE}/organizations/Ares-Fighting-Championship-15823',
        'slug_keys':    ('ares-',),
        'exclude_slug_keys': ('ares-chaos',),
        'name_clean':   (r'^$', ''),
        'fight_prefix': 'aresfight',
        'active':       True,
    },
    {
        # Urijah Faber's Sacramento promotion (2021-present), ~43 events +
        # Prospect Series / Combat U sub-cards. FP: "... Urijah Faber's A-1
        # Combat 36" (hyphenated on FP, "A1" on Sherdog). 'fabers-a1-combat'
        # slug key avoids the unrelated "A1 Combate MMA" org.
        'organization': 'A1 Combat',
        'org_url':      f'{SHERDOG_BASE}/organizations/Urijah-Fabers-A1-Combat-17054',
        'slug_keys':    ('fabers-a1-combat',),
        'name_clean':   (r'^$', ''),
        'fight_prefix': 'a1fight',
        'active':       True,
    },
    {
        # Mexico's UWC (2010s-present), ~61 events. Fight Pass titles drop
        # "Mexico" ("X vs Y UWC 58") — strip it from names so hints match.
        'organization': 'UWC',
        'org_url':      f'{SHERDOG_BASE}/organizations/Ultimate-Warrior-Challenge-Mexico-2611',
        'slug_keys':    ('uwc-mexico-',),
        'name_clean':   (r'^UWC\s+Mexico', 'UWC'),
        'fight_prefix': 'uwcfight',
        'active':       True,
    },
    {
        # Mexico City promotion (2018-present), ~63 events. Sherdog zero-pads
        # event numbers ("LUX 062") while Fight Pass says "Lux Fight League 56"
        # — strip the padding so numbers line up.
        'organization': 'LUX',
        'org_url':      f'{SHERDOG_BASE}/organizations/LUX-Fight-League-12569',
        'slug_keys':    ('lux-',),
        'name_clean':   (r'^LUX\s+0+(\d)', r'LUX \1'),
        'fight_prefix': 'luxfight',
        'active':       True,
    },
    {
        # Jue Cheng King (China, ~143 events incl. "JCK Fight Night N" and
        # "JCK Kings NNN"). FP titles: "X vs Y JCK MMA 2023: ...". Strip the
        # redundant "Jue Cheng King - " prefix.
        'organization': 'JCK',
        'org_url':      f'{SHERDOG_BASE}/organizations/Jue-Cheng-King-16251',
        'slug_keys':    ('jue-cheng-king',),
        'name_clean':   (r'^Jue\s+Cheng\s+King\s*-\s*', ''),
        'fight_prefix': 'jckfight',
        'active':       True,
    },
    {
        # Anthony Pettis FC (2021-present), ~25 events incl. the India series.
        # FP: "X vs Y APFC: ..." / "X vs Y Anthony Pettis FC 9".
        'organization': 'APFC',
        'org_url':      f'{SHERDOG_BASE}/organizations/Anthony-Pettis-Fighting-Championship-APFC-16795',
        'slug_keys':    ('anthony-pettis-fc',),
        'name_clean':   (r'^$', ''),   # names already "Anthony Pettis FC 23 - ..."
        'fight_prefix': 'apfcfight',
        'active':       True,
    },
    {
        # Xtreme Fighting League (Tulsa, OK) — runs the "Xtreme Fight Night"
        # (XFN) series Fight Pass carries ("X vs Y XFN 383"). Sherdog names
        # events "XFL - Xtreme Fight Night 411" — rewrite to "XFN 411" so
        # Fight Pass hints match. Mixed MMA/kickboxing cards; kickboxing
        # bouts aren't indexed by Sherdog so only MMA comes through.
        'organization': 'XFN',
        'org_url':      f'{SHERDOG_BASE}/organizations/Xtreme-Fighting-League-Oklahoma-575',
        'slug_keys':    ('xfl-', 'xtreme-fight-night'),
        'name_clean':   (r'^XFL\s*-\s*Xtreme\s+Fight\s+Night', 'XFN'),
        'fight_prefix': 'xfnfight',
        'active':       True,
    },
    {
        # Legacy FC (Texas, 2009-2016) — merged with RFA to form LFA. 66
        # events; Fight Pass has per-fight VODs ("X vs Y Legacy FC 63").
        # Sherdog names duplicate the abbrev ("LFC 63 - Legacy Fighting
        # Championship 63") — rewrite to "Legacy FC 63". 'cage-legacy' is a
        # different org.
        'organization': 'Legacy FC',
        'org_url':      f'{SHERDOG_BASE}/organizations/Legacy-Fighting-Championship-Texas-2602',
        'slug_keys':    ('lfc-', 'legacy-'),
        'exclude_slug_keys': ('cage-legacy',),
        'name_clean':   (r'^LFC\s+(\d+)\s*-\s*Legacy\s+Fighting\s+Championship\s+\d+',
                         r'Legacy FC \1'),
        'fight_prefix': 'legacyfight',
        'active':       False,
    },
    {
        # Classic Entertainment & Sports (New England, 2010-present), ~85
        # events. Fight Pass has event replays + per-fight VODs ("CES MMA 73").
        'organization': 'CES MMA',
        'org_url':      f'{SHERDOG_BASE}/organizations/Classic-Entertainment-and-Sports-CES-MMA-3124',
        'slug_keys':    ('ces-mma-',),
        'name_clean':   (r'^$', ''),   # names already "CES MMA 82 - ..."
        'fight_prefix': 'cesfight',
        'active':       True,
    },
    {
        # Jorge Masvidal's iKON FC (2022-2023, 6 events) + the earlier iKon
        # Fighting Federation era (iFF 1-7) under the same Sherdog org. FP
        # has event replays ("Jorge Masvidal's iKON FC 6"). Strip the
        # redundant "iFC N - " / "iFF N - " prefix.
        'organization': 'iKON FC',
        'org_url':      f'{SHERDOG_BASE}/organizations/iKon-Fighting-Championship-16451',
        'slug_keys':    ('ifc-', 'iff-'),
        'name_clean':   (r'^iF[CF]\s+\d+\s*-\s*', ''),
        'fight_prefix': 'ikonfight',
        'active':       False,
    },
    # ── 2026-07 YouTube-video wave ────────────────────────────────────────────
    # Orgs with no Fight Pass content but a reliable official YouTube channel
    # posting full fights (matched in youtube_scraper.py, RIZIN-style).
    {
        # ONE Championship (Singapore, 2011-present). Top-3 global promotion,
        # 400+ events. Sherdog files every ONE event under this org with slugs
        # 'One-Championship-...' (Friday Fights, on Prime Video, Samurai, etc.);
        # the org page also links related PFL/UFC events, excluded by slug key.
        # Names come as "One Championship - One Friday Fights 161" — strip the
        # redundant "One Championship - " prefix. ONE cards mix MMA with Muay
        # Thai/kickboxing/grappling, but Sherdog only indexes the MMA bouts, so
        # only MMA comes through. Videos via the official ONE YouTube channel.
        'organization': 'ONE Championship',
        'org_url':      f'{SHERDOG_BASE}/organizations/One-Championship-3877',
        'slug_keys':    ('one-championship-',),
        'name_clean':   (r'^One Championship\s*-\s*', ''),
        'fight_prefix': 'onefight',
        'active':       True,
    },
    {
        # Brave Combat Federation (Bahrain, 2016-present). ~107 events across
        # 38 countries — the 'most global' promotion. Sherdog slugs are
        # 'Brave-CF-NNN-Brave-Combat-Federation-NNN...' (joint events append
        # '/ WFC 30', '/ Alash Pride 123', etc.). Names duplicate the abbrev
        # ("Brave CF 101 - Brave Combat Federation 101") — strip the trailing
        # "- Brave Combat Federation ..." copy down to "Brave CF 101". Videos
        # via the official BRAVE CF YouTube channel.
        'organization': 'Brave CF',
        'org_url':      f'{SHERDOG_BASE}/organizations/Brave-Combat-Federation-11227',
        'slug_keys':    ('brave-cf-',),
        'name_clean':   (r'\s*-\s*Brave\s+Combat\s+Federation.*$', ''),
        'fight_prefix': 'bravefight',
        'active':       True,
    },
    # ── 2026-08-15 Fight Pass survey wave ────────────────────────────────────
    {
        # Shooto Brazil (1994-present), ~138 events. Its own Sherdog org —
        # DISTINCT from Japanese Shooto (Shooto-21), whose org page holds no
        # Brazil events. Active Fight Pass partner with per-fight VODs.
        'organization': 'Shooto Brazil',
        'org_url':      f'{SHERDOG_BASE}/organizations/Shooto-Brazil-13715',
        'slug_keys':    ('shooto-brazil-',),
        'name_clean':   (r'^$', ''),   # names already "Shooto Brazil 138 - ..."
        'fight_prefix': 'shootobrfight',
        'active':       True,
    },
    {
        # Battlefield Fight League (Vancouver, 2010-present), Canada's largest
        # promotion, ~88 events. FP has per-fight clips AND full-event replays.
        # Names duplicate the abbrev ("BFL 68 - Battlefield Fight League").
        'organization': 'BFL',
        'org_url':      f'{SHERDOG_BASE}/organizations/Battlefield-Fight-League-3067',
        'slug_keys':    ('bfl-',),
        'name_clean':   (r'\s*-\s*Battlefield\s+Fight\s+League.*$', ''),
        'fight_prefix': 'bflfight',
        'active':       True,
    },
    {
        # Tuff-N-Uff (Las Vegas, 1994-present), ~166 events. Long-time Fight
        # Pass partner. Historically amateur-heavy — Sherdog lists the pro
        # bouts; events with no pro fights get pruned as orphans. Names come
        # as "Tuff-N-Uff - Tuff-N-Uff 154" — strip the leading copy.
        'organization': 'Tuff-N-Uff',
        'org_url':      f'{SHERDOG_BASE}/organizations/TuffNUff-972',
        'slug_keys':    ('tuffnuff',),
        'name_clean':   (r'^Tuff-N-Uff\s*-\s*', ''),
        'fight_prefix': 'tuffnuffight',
        'active':       True,
    },
    {
        # Unified MMA (Edmonton, 2009-present), ~68 events. FP per-fight VODs.
        # Names duplicate the org ("Unified MMA 63 - Unified MMA").
        'organization': 'Unified MMA',
        'org_url':      f'{SHERDOG_BASE}/organizations/Unified-MMA-2802',
        'slug_keys':    ('unified-mma-',),
        'name_clean':   (r'\s*-\s*Unified\s+MMA$', ''),
        'fight_prefix': 'unifiedfight',
        'active':       True,
    },
    {
        # INKA MMA (Lima, Peru), ~59 events. FP per-fight VODs (Spanish +
        # English live feeds). Sherdog names are "Inka 55 - Night of
        # Prospects" — kept as-is; FP's "Inka MMA 59" matches via noise-token
        # stripping ('mma' is a noise token).
        'organization': 'INKA MMA',
        'org_url':      f'{SHERDOG_BASE}/organizations/Inka-MMA-1673',
        'slug_keys':    ('inka-',),
        'name_clean':   (r'^$', ''),
        'fight_prefix': 'inkafight',
        'active':       True,
    },
    {
        # Bison Kombat (Brazil, 2023-present), 8 events so far, growing FP
        # partner. Mixed pro/amateur cards — Sherdog lists the pro bouts.
        'organization': 'Bison Kombat',
        'org_url':      f'{SHERDOG_BASE}/organizations/Bison-Kombat-10127',
        'slug_keys':    ('bison-kombat-',),
        'name_clean':   (r'^$', ''),
        'fight_prefix': 'bisonfight',
        'active':       True,
    },
    {
        # ZFN / Z Fight Night (Seoul, 2025-present) — the Korean Zombie's
        # promotion, Korea's FP partner. Small but growing ("ZFN - Origin N"
        # events; FP titles zero-pad: 'ZFN Origin 04').
        'organization': 'ZFN',
        'org_url':      f'{SHERDOG_BASE}/organizations/ZFight-Night-18328',
        'slug_keys':    ('zfn-',),
        'name_clean':   (r'^$', ''),
        'fight_prefix': 'zfnfight',
        'active':       True,
    },
    {
        # Fighting Alliance Championship (Kansas City, 2019-present), ~36
        # events, still running (FAC 36 upcoming as of 2026-08). FP has
        # full-event replays + per-fight clips. Names duplicate the abbrev
        # ("FAC 36 - Fighting Alliance Championship 36") — strip the trailing
        # copy.
        'organization': 'FAC',
        'org_url':      f'{SHERDOG_BASE}/organizations/Fighting-Alliance-Championship-15429',
        'slug_keys':    ('fac-',),
        'name_clean':   (r'\s*-\s*Fighting\s+Alliance\s+Championship.*$', ''),
        'fight_prefix': 'facfight',
        'active':       True,
    },
    {
        # Warrior Xtreme Cagefighting / Warrior Xtreme Combat (Michigan,
        # 2008-present), ~48 events (WXC 99 late 2025). Early Khaos Williams /
        # Josh Parisian era lives on FP (event replays + clips). Names like
        # "WXC 99 - Seasons Beatings" are already clean.
        'organization': 'WXC',
        'org_url':      f'{SHERDOG_BASE}/organizations/Warrior-Xtreme-Cagefighting-6015',
        'slug_keys':    ('wxc-',),
        'name_clean':   (r'^$', ''),
        'fight_prefix': 'wxcfight',
        'active':       True,
    },
    {
        # Alaska Fighting Championship (Anchorage, 2003-present), ~186 events
        # (AFC 175 in 2026). FP has event replays + clips for the 155-157 era.
        # Names duplicate the abbrev ("AFC 175 - Alaska Fighting Championship
        # 175") — strip the trailing copy.
        'organization': 'Alaska FC',
        'org_url':      f'{SHERDOG_BASE}/organizations/Alaska-Fighting-Championship-492',
        'slug_keys':    ('afc-',),
        'name_clean':   (r'\s*-\s*Alaska\s+Fighting\s+Championship.*$', ''),
        'fight_prefix': 'afcfight',
        'active':       True,
    },
]

# ── Supabase REST helpers ─────────────────────────────────────────────────────

def _headers():
    return {
        'apikey': SUPABASE_KEY,
        'Authorization': f'Bearer {SUPABASE_KEY}',
        'Content-Type': 'application/json',
        'Prefer': 'resolution=merge-duplicates,return=minimal',
    }

def upsert(table, records):
    """Upsert one or more records into a Supabase table."""
    if not records:
        return
    if isinstance(records, dict):
        records = [records]
    resp = requests.post(
        f'{SUPABASE_URL}/rest/v1/{table}',
        headers=_headers(),
        data=json.dumps(records),
    )
    if resp.status_code not in (200, 201):
        # Raise — a failed push must abort before events get marked completed.
        raise RuntimeError(
            f'upsert {table} failed ({resp.status_code}): {resp.text[:300]}')

def fetch_female_fighter_names():
    """Lowercased names of fighters known to be female (for women's labels)."""
    names = set()
    offset = 0
    while True:
        resp = requests.get(
            f'{SUPABASE_URL}/rest/v1/fighters'
            f'?gender=eq.F&select=name&limit=1000&offset={offset}',
            headers=_headers(),
        )
        if not resp.ok:
            break
        batch = resp.json()
        names.update(r['name'].lower() for r in batch)
        if len(batch) < 1000:
            break
        offset += 1000
    return names

def rpc(name):
    """Call a parameterless Supabase stored procedure."""
    resp = requests.post(
        f'{SUPABASE_URL}/rest/v1/rpc/{name}',
        headers=_headers(),
        data='{}',
    )
    if resp.status_code not in (200, 204):
        print(f'  [WARN] rpc {name} failed ({resp.status_code}): {resp.text[:200]}')
        return None
    try:
        return resp.json()
    except Exception:
        return None

# ── ID helpers ────────────────────────────────────────────────────────────────
# Source of truth is Sherdog's own stable numeric ids (parsed from URLs):
#   fighters  -> /fighter/Name-NNNNN
#   events    -> /events/slug-NNNNN
#   orgs      -> /organizations/Name-NNN  (hardcoded in SHERDOG_ORGS)
# fighters.id / events.id are those bigints. fights.id is a deterministic text
# hash of event_id + the sorted fighter ids — one scheme for UFC and non-UFC
# alike (numeric event ids are globally unique, so no org prefix is needed).

def id_from_url(url):
    """Trailing numeric id off a Sherdog URL/href ('.../Name-12345' -> 12345)."""
    if not url:
        return None
    m = re.search(r'-(\d+)/?$', url.strip())
    return int(m.group(1)) if m else None

def sherdog_fight_id(event_id, fighter_a_id, fighter_b_id):
    """Deterministic fight id — Sherdog has no per-fight URL like ufcstats."""
    a, b = sorted([fighter_a_id or 0, fighter_b_id or 0])
    return str(uuid.uuid5(_NS, f"{event_id}:{a}:{b}"))

def split_method(raw):
    """'KO (Punch)' -> ('KO', 'Punch');  'Decision (Unanimous)' -> ('Decision', 'Unanimous')."""
    if not raw:
        return None, None
    m = re.match(r'^(.+?)\s*\((.+)\)\s*$', raw)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return raw.strip(), None

# ── Incremental tracking ──────────────────────────────────────────────────────

def load_completed_events():
    if not os.path.exists(COMPLETED_EVENTS_FILE):
        return set()
    with open(COMPLETED_EVENTS_FILE) as f:
        return set(line.strip() for line in f if line.strip())

def mark_event_completed(url):
    with open(COMPLETED_EVENTS_FILE, 'a') as f:
        f.write(url + '\n')

# ── Scraping ──────────────────────────────────────────────────────────────────

def _fetch(url, retries=4):
    """GET a Sherdog page with a browser UA, retrying transient network errors."""
    last = None
    for attempt in range(retries):
        try:
            return requests.get(url, headers={'User-Agent': USER_AGENT},
                                allow_redirects=True, timeout=30)
        except requests.exceptions.RequestException as e:
            last = e
            time.sleep(2 * (attempt + 1))
    raise last

def get_event_urls(org):
    """
    Fetch one org's event URLs from its Sherdog organization page, following
    the 'Older Events' pagination (/recent-events/N) to the end.
    The org page also links 'related events' from other promotions — keep
    only slugs matching this org's keywords.
    """
    urls = []
    seen = set()
    page_url = org['org_url']
    visited = set()

    while page_url and page_url not in visited:
        visited.add(page_url)
        resp = _fetch(page_url)
        soup = BeautifulSoup(resp.text, 'html.parser')

        for a in soup.select('a[href^="/events/"]'):
            href = a.get('href')
            if not href or href in seen:
                continue
            slug = href.split('/events/', 1)[-1].lower()
            if (any(k in slug for k in org['slug_keys'])
                    and not any(k in slug for k in org.get('exclude_slug_keys', ()))):
                seen.add(href)
                urls.append(SHERDOG_BASE + href)

        # Follow the "Older Events" pagination link, if any.
        page_url = None
        pag = soup.select_one('.pagination')
        if pag:
            for a in pag.find_all('a', href=True):
                if 'older' in a.get_text(strip=True).lower():
                    page_url = SHERDOG_BASE + a['href']
                    break
        time.sleep(0.3)

    return urls


def _clean_name(node):
    """Pull a fighter name out of a node, joining first/last split by <br>."""
    if not node:
        return None
    name = node.get_text(separator=' ', strip=True)
    return re.sub(r'\s+', ' ', name).strip() or None


def _fighter_from_anchor(anchor):
    """(name, sherdog_id) from an a[href^='/fighter/'] node."""
    if not anchor:
        return None, None
    return _clean_name(anchor), id_from_url(anchor.get('href'))


def _is_amateur(weight_class):
    """Sherdog flags amateur bouts in the weight-class text — exclude them."""
    return bool(weight_class) and 'amateur' in weight_class.lower()


def _is_title(weight_class, *nodes):
    """Title bout if Sherdog tags it (span.title_fight), the weight-class text
    says so, or a belt icon is present."""
    if weight_class and ('title' in weight_class.lower()
                         or 'championship' in weight_class.lower()):
        return True
    for n in nodes:
        if not n:
            continue
        if n.select_one('.title_fight'):
            return True
        if n.find('img', src=lambda s: s and 'belt' in s.lower()):
            return True
    return False


def _iso_to_text(iso):
    """'2026-04-11T00:00:00+00:00' -> 'April 11, 2026' (matches UFC date format)."""
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso[:10]).strftime('%B %d, %Y').replace(' 0', ' ')
    except ValueError:
        return None


def parse_event_page(event_url, org):
    """
    Scrape one Sherdog event page. Returns a list of fight dicts — one per fight.
    fight_position 1 = main event. Upcoming fights have None for result fields.
    """
    resp = _fetch(event_url)
    if resp.status_code != 200:
        print(f'  SKIP (HTTP {resp.status_code}): {event_url}')
        return []

    soup = BeautifulSoup(resp.text, 'html.parser')

    # ── Event metadata ────────────────────────────────────────────────────────
    name_el = soup.find(itemprop='name')
    event_name = name_el.get_text(strip=True) if name_el else 'Unknown'
    pattern, replacement = org['name_clean']
    event_name = re.sub(pattern, replacement, event_name).strip()

    date_el = soup.find('meta', itemprop='startDate')
    iso = date_el.get('content') if date_el else None
    event_date = iso[:10] if iso else None        # ISO YYYY-MM-DD for date column

    loc_el = soup.find(itemprop='location')
    event_location = loc_el.get_text(strip=True) if loc_el else None

    # Amateur cards are whole events on Sherdog (e.g. "Fury FC - Amateur
    # Series 60") — the bouts carry normal weight classes, so the signal is the
    # event name. We KEEP these fights but flag them is_amateur (hidden in-app).
    event_amateur = bool(re.search(r'\bamateur\b', event_name, re.I))

    base = {
        'organization':    org['organization'],
        'organization_id': id_from_url(org['org_url']),
        'event_id':        id_from_url(event_url),
        'event_name':      event_name,
        'event_date':      event_date,
        'event_location':  event_location,
    }

    fights = []

    # ── Main event (in .fight_card) ───────────────────────────────────────────
    fc = soup.select_one('.fight_card')
    if fc:
        # name comes from itemprop (reliable); id from the fighter anchor.
        m = []
        for d in fc.select('.fighter')[:2]:
            a = d.select_one('a[href^="/fighter/"]')
            name = _clean_name(d.select_one('[itemprop="name"]')) or _clean_name(a)
            m.append((name, id_from_url(a.get('href')) if a else None))

        wc_el = fc.select_one('.weight_class')
        weight_class = wc_el.get_text(strip=True) if wc_el else None

        results = fc.select('.final_result')
        winner_idx = None
        if len(m) == 2 and results:
            if 'win' in results[0].get('class', []):
                winner_idx = 0
            elif len(results) > 1 and 'win' in results[1].get('class', []):
                winner_idx = 1

        method = finish_round = finish_time = None
        resume = soup.select_one('.fight_card_resume')
        if resume:
            cells = {}
            for td in resume.find_all('td'):
                em = td.find('em')
                if em:
                    label = em.get_text(strip=True)
                    value = td.get_text(strip=True).replace(label, '', 1).strip()
                    cells[label] = value
            method       = cells.get('Method') or None
            finish_round = cells.get('Round') or None
            finish_time  = cells.get('Time') or None

        if len(m) == 2 and m[0][0] and m[1][0]:
            fights.append({
                **base,
                'fight_position': 1,
                'fighter_a':      m[0][0],
                'fighter_a_id':   m[0][1],
                'fighter_b':      m[1][0],
                'fighter_b_id':   m[1][1],
                'winner_id':      m[winner_idx][1] if winner_idx is not None else None,
                'weight_class':   weight_class,
                'is_title':       _is_title(weight_class, fc),
                'is_amateur':     event_amateur or _is_amateur(weight_class),
                'method':         method,
                'finish_round':   finish_round,
                'finish_time':    finish_time,
            })

    # ── Undercard table ───────────────────────────────────────────────────────
    table = soup.select_one('table.new_table')
    if table:
        rows = table.find_all('tr')
        position = len(fights)  # main event already counted
        for tr in rows[1:]:  # skip header
            cells = tr.find_all('td')
            # Completed events: 7 cols (incl. Method/R/Time).
            # Upcoming events: 5 cols (Match, Fighter, Weight, Fighter, blank).
            if len(cells) < 5:
                continue

            fa, fa_id = _fighter_from_anchor(cells[1].select_one('a[href^="/fighter/"]'))
            fb, fb_id = _fighter_from_anchor(cells[3].select_one('a[href^="/fighter/"]'))
            if not fa or not fb:
                continue

            wc_el = cells[2].select_one('.weight_class')
            weight_class = wc_el.get_text(strip=True) if wc_el else None
            position += 1

            res_a = cells[1].select_one('.final_result')
            res_b = cells[3].select_one('.final_result')
            winner_id = None
            if res_a and 'win' in res_a.get('class', []):
                winner_id = fa_id
            elif res_b and 'win' in res_b.get('class', []):
                winner_id = fb_id

            # Result columns only exist on completed-event tables.
            method = rnd = t = None
            if len(cells) >= 7:
                method_el = cells[4].find('b')
                method = method_el.get_text(strip=True) if method_el else None
                rnd  = cells[5].get_text(strip=True)
                t    = cells[6].get_text(strip=True)

            fights.append({
                **base,
                'fight_position': position,
                'fighter_a':      fa,
                'fighter_a_id':   fa_id,
                'fighter_b':      fb,
                'fighter_b_id':   fb_id,
                'winner_id':      winner_id,
                'weight_class':   weight_class,
                'is_title':       _is_title(weight_class, tr),
                'is_amateur':     event_amateur or _is_amateur(weight_class),
                'method':         method or None,
                'finish_round':   rnd or None,
                'finish_time':    t or None,
            })

    return fights

# ── Supabase push ─────────────────────────────────────────────────────────────

def push_fights(all_fights, female_names):
    """Batch upsert events, fighters, fights, fight_results — keyed on Sherdog
    numeric ids. (organizations are upserted once in main().)"""
    events_map   = {}
    fighters_map = {}
    fights_map   = {}   # keyed by fight id — dedupes within the batch
    results_map  = {}   # keyed by fight id

    for f in all_fights:
        ev_id = f['event_id']
        a_id  = f['fighter_a_id']
        b_id  = f['fighter_b_id']
        w_id  = f['winner_id']
        # Every row must carry stable numeric ids; without them we can't key it.
        if not (ev_id and a_id and b_id):
            print(f"  [WARN] missing Sherdog id, skipping: "
                  f"{f['fighter_a']} vs {f['fighter_b']} @ {f['event_name']}")
            continue
        fid = sherdog_fight_id(ev_id, a_id, b_id)

        # Sherdog doesn't gender-label weight classes — prefix "Women's " when
        # a fighter is known female. recalculate_womens_divisions() backfills
        # any debutant women the female set doesn't yet cover.
        weight_class = f['weight_class']
        if weight_class and not weight_class.lower().startswith("women"):
            if ((f['fighter_a'] or '').lower() in female_names or
                    (f['fighter_b'] or '').lower() in female_names):
                weight_class = f"Women's {weight_class}"

        events_map[ev_id] = {
            'id':              ev_id,
            'name':            f['event_name'],
            'date':            f['event_date'],
            'location':        f['event_location'],
            'organization':    f['organization'],
            'organization_id': f['organization_id'],
        }

        for name, uid in [(f['fighter_a'], a_id), (f['fighter_b'], b_id)]:
            if name and uid:
                fighters_map[uid] = {'id': uid, 'name': name}

        # Keep first occurrence — a fight reached via two redirect-equivalent
        # event URLs would otherwise collide on id within the upsert batch.
        if fid not in fights_map:
            fights_map[fid] = {
                'id':                  fid,
                'event_id':            ev_id,
                'fighter1_id':         a_id,
                'fighter2_id':         b_id,
                'weight_class':        weight_class,
                'is_main':             f['fight_position'] == 1,
                'is_title':            f.get('is_title', False),
                'is_amateur':          f.get('is_amateur', False),
                'fight_position':      f['fight_position'],
                'fight_position_type': 'Main Event' if f['fight_position'] == 1 else None,
            }

        # A finished fight has a method; upcoming ones don't.
        if f['method'] and fid not in results_map:
            method, details = split_method(f['method'])
            results_map[fid] = {
                'fight_id':  fid,
                'winner_id': w_id,
                'method':    method,
                'round':     int(f['finish_round']) if (f['finish_round'] or '').isdigit() else None,
                'time':      f['finish_time'],
                'details':   details,
            }

    upsert('events',        list(events_map.values()))
    upsert('fighters',      list(fighters_map.values()))
    upsert('fights',        list(fights_map.values()))
    upsert('fight_results', list(results_map.values()))
    prune_stale_fights(fights_map)

def prune_stale_fights(fights_map):
    """Delete DB fights that dropped off a re-scraped card. Fights are keyed
    by (event, fighterA, fighterB), so an opponent swap inserts the new
    pairing but leaves the old one behind — upserts never remove rows (e.g.
    Hardwick vs Hughes lingering on Cage Warriors 209 after the Savio swap).

    Scoped to the events in this batch. A fight is never deleted if it has a
    fight_results row (it actually happened) or a ratings row (user data);
    both FKs are NO ACTION, so those deletes would fail anyway."""
    by_event = {}
    for f in fights_map.values():
        by_event.setdefault(f['event_id'], set()).add(f['id'])

    deleted = 0
    for ev_id, keep_ids in by_event.items():
        resp = requests.get(
            f'{SUPABASE_URL}/rest/v1/fights?event_id=eq.{ev_id}&select=id',
            headers=_headers())
        if not resp.ok:
            continue
        stale = [r['id'] for r in resp.json() if r['id'] not in keep_ids]
        if not stale:
            continue
        protected = set()
        for table in ('fight_results', 'ratings'):
            r = requests.get(
                f'{SUPABASE_URL}/rest/v1/{table}'
                f'?fight_id=in.({",".join(stale)})&select=fight_id',
                headers=_headers())
            if r.ok:
                protected.update(x['fight_id'] for x in r.json())
            else:
                protected.update(stale)   # can't verify — keep everything
        doomed = [i for i in stale if i not in protected]
        if not doomed:
            continue
        r = requests.delete(
            f'{SUPABASE_URL}/rest/v1/fights?id=in.({",".join(doomed)})',
            headers=_headers())
        if r.status_code in (200, 204):
            deleted += len(doomed)
        else:
            print(f'  [WARN] prune for event {ev_id} failed '
                  f'({r.status_code}): {r.text[:200]}')
    if deleted:
        print(f'  Pruned {deleted} stale fight(s) no longer on their card')

# ── Main ──────────────────────────────────────────────────────────────────────

def upsert_organizations():
    """Upsert the organizations lookup (FK target for events.organization_id).
    Deduped by Sherdog org id — UFC and Road to UFC share org id 2, so the
    first config (UFC) wins; RTUFC events still keep their text label in
    events.organization."""
    orgs = {}
    for o in SHERDOG_ORGS:
        oid = id_from_url(o['org_url'])
        if oid and oid not in orgs:
            orgs[oid] = {
                'id':     oid,
                'name':   o['organization'],
                'status': 'active' if o.get('active', True) else 'defunct',
            }
    upsert('organizations', list(orgs.values()))


def main(include_inactive=False, limit=None, only_orgs=None):
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise ValueError('Missing SUPABASE_URL or SUPABASE_KEY in .env file')

    completed = load_completed_events()
    female_names = fetch_female_fighter_names()
    print(f'Loaded {len(female_names)} known female fighters')

    # Organizations are the FK target for events — upsert them first.
    upsert_organizations()

    total_pushed = 0
    remaining = limit  # None = no cap; otherwise max events to scrape this run

    if only_orgs:
        wanted = {o.strip().lower() for o in only_orgs}
        orgs = [o for o in SHERDOG_ORGS if o['organization'].lower() in wanted]
    else:
        orgs = [o for o in SHERDOG_ORGS if o.get('active', True) or include_inactive]

    for org in orgs:
        if remaining is not None and remaining <= 0:
            break
        print(f'\n=== {org["organization"]} ===')
        print('Fetching event list from Sherdog...')
        event_urls = get_event_urls(org)
        to_scrape = [u for u in event_urls if u not in completed]
        if remaining is not None:
            to_scrape = to_scrape[:remaining]
        print(f'  {len(event_urls)} events found, '
              f'{len(event_urls) - len([u for u in event_urls if u not in completed])} already scraped, '
              f'scraping {len(to_scrape)}...')

        # Accumulate + push PER ORG so a network failure mid-run only loses the
        # current org, not every org scraped before it.
        org_fights = []
        org_completed = []
        for i, url in enumerate(to_scrape):
            print(f'  [{i+1}/{len(to_scrape)}] {url}')
            fights = parse_event_page(url, org)
            if fights:
                org_fights.extend(fights)
                # An event is "completed" once every fight has a result.
                if all(f['method'] for f in fights):
                    org_completed.append(url)
            time.sleep(0.5)

        if org_fights:
            print(f'  Pushing {len(org_fights)} {org["organization"]} fights...')
            push_fights(org_fights, female_names)
            for url in org_completed:
                mark_event_completed(url)
            total_pushed += len(org_fights)

        if remaining is not None:
            remaining -= len(to_scrape)

    if total_pushed:
        print('\nRemoving orphan events...')
        rpc('delete_orphan_events')
        print('Recalculating debut flags...')
        rpc('recalculate_debut_flags')
        print('Recalculating women\'s divisions...')
        rpc('recalculate_womens_divisions')

    print(f'\nDone — pushed {total_pushed} fights to Supabase.')

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--all', action='store_true',
                        help='Include inactive (defunct) orgs like Bellator/WSOF')
    parser.add_argument('--limit', type=int, default=None,
                        help='Max events to scrape this run (chunked/resumable backfill)')
    parser.add_argument('--orgs', type=str, default=None,
                        help='Comma-separated org names to scrape (overrides active filter), '
                             'e.g. --orgs "UFC,PFL,MVP"')
    args = parser.parse_args()
    only = args.orgs.split(',') if args.orgs else None
    main(include_inactive=args.all, limit=args.limit, only_orgs=only)
