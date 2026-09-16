# MMA Scraper — Backlog

Future work, roughly prioritized. Each item carries notes on the intended
approach so it can be picked up cold in a later session.

## North-star vision

A complete canonical record of **every professional MMA event** — every org,
every era — so that looking up a **fighter** shows their **entire fight
history**, with **video links on any fight that has one**.

Event-centric: events are the unit, fights belong to events. Recording every
pro event automatically yields complete fighter histories (a fighter's record
is just the set of fights they appear in).

### Roadmap

**Phase 1 — Sherdog-ID migration**  *(gating step)*
- Move fighters/events/fights off name-hashed UUIDs onto Sherdog's stable
  numeric IDs, **in place** on the existing DB (do NOT fork to a new project —
  ratings must ride along, never be "imported later").
- Why: at full scale, name-hash IDs silently merge different fighters/events
  that share a name+date (a real bug, possibly already latent).
- Ratings re-point within the same DB (same technique as the MVP migration).
- Do this on the current 5 orgs first — prove it before scaling.

**Phase 2 — Full event backfill**
- Enumerate **every pro event** on Sherdog. Open question to settle first:
  crawl Sherdog's global events index vs. enumerate every organization
  (the `/recent-events/N` pagination already exists for the org route).
- Exclude **amateur** events/fights (Sherdog indexes them too).
- Chunked, resumable backfill — tens of thousands of events, well past a single
  GitHub Actions job. Disable the `change_log` audit triggers during backfill.
- Daily job afterwards stays incremental (new events only).

**Phase 3 — Video linking continues**
- Paramount / ESPN / YouTube scrapers keep accreting links over time.
- Most historical fights will never have video — expected; the profile just
  flags the ones that do.

**Phase 4 — Fighter profile page**  *(app-side, frontend)*
- Look up fighter → full fight list → video links. Not gated on Phases 1–3 —
  can be built against current data and improves as the backend fills in.

### Build approach — staging copy, not a live migration
The Phase-1 ID migration is the riskiest step; never run it live against the
DB that holds the ratings. Build it on the side:
1. Stand up a **second Supabase project** as staging; `pg_dump` the production
   DB and restore into it — a full copy, **ratings included**.
2. Point scrapers/migration at staging via a different `SUPABASE_URL` in
   `.env` — same repo, no code fork. Production + app keep running untouched.
3. Do all the risky work on staging — break/rebuild/re-copy freely.
4. Write the migration as a **repeatable script**, not hand-done.
5. Cutover (a planned event): fresh-copy production → staging, re-run the
   scripted migration, validate, repoint the app. Only reconcile the small
   delta of ratings logged during the build window.

End state is still **one DB, one app** — staging is a build environment, not a
permanent fork.

### Watch-outs
- Supabase free tier is 500 MB — a full backfill will likely need a paid tier.
- Sherdog data quality drops at the regional long tail (messy names, missing
  results, duplicate profiles) — more review-queue load.

## Organizations

### DONE — Bellator, WSOF  (added 2026-05-17)
- Bellator (315 events) + WSOF (37 events) scraped from Sherdog, both marked
  `active: false` in `SHERDOG_ORGS` (defunct — one-time scrape, skipped daily).
- Re-run with `python sherdog_scraper.py --all` if ever needed.

- **Non-MMA events under Bellator (found 2026-09-06).** Sherdog's Bellator
  promoter page carries five NASCAR races that were scraped as events:
  "2014 Monster Energy Cup" (41541), "Monster Energy All-Star Race" (59155),
  "Bass Pro Shops NRA Night Race" (59157), "Alabama 500" (59159), "Ford
  EcoBoost 400" (59161). Delete them (and add their ids to a skip list in
  `sherdog_scraper.py` so a re-scrape doesn't resurrect them).

### TODO — PRIDE / Strikeforce / WEC (and other Fight Pass orgs)
Incorporate every org with content on Fight Pass into the DB. Two halves:
1. **Fight records** — extend `sherdog_scraper.py` with new `SHERDOG_ORGS`
   entries (Sherdog has full PRIDE / Strikeforce / WEC event histories). Use
   distinct `fight_prefix` per org (e.g. `pridefight`, `sffight`, `wecfight`)
   so IDs stay stable forever — see ground rule below.
2. **Video scraper** — Fight Pass already exposes these via top-level buckets
   I've already enumerated. Extend `PLAYLIST_BUCKETS` in `fightpass_scraper.py`:
   - `uj-A` — Pride FC Event Replays
   - `97Ms` — StrikeForce Event Replays
   - `_7Cg` — WEC Event Replays
   - `ioe3` — Pride FC: Classic Fights (per-fight VODs)
   - `JW-A` — Strikeforce: Classic Fights
   - `cbRY` — WEC: Classic Fights
   - `Md4s` — PRIDE FC LEGENDS
   The existing `--scan-vods` brute-force pass will also pick up standalone
   fight VODs for these orgs once the fighters/fights exist in the DB
   (matching is fighter-pair based, org-agnostic).

### Other orgs worth evaluating
Once the fighter-page scraper (Phase 2) lands, explicit org configs matter much
less — fighter pages pull in every org anyway. Still useful for events/video:
- ~~**ONE Championship**~~ (done 2026-07-10 — Sherdog `One-Championship-3877`,
  video via YouTube), ~~**Cage Warriors**, **Invicta FC**, **RIZIN**~~ (all
  done). Still open: **KSW** (Sherdog-only — paywalled video), **Oktagon**.

### Fight Pass × Sherdog org expansion roadmap

Survey done 2026-06-19 — re-walked the 17 top-level Fight Pass buckets and
vesper-searched 34 org names; cross-referenced with a Sherdog org-ID scan.
Buckets with ≥20 "X vs Y" titles for the org name are real per-fight content;
buckets with 0–4 are noise / namesakes.

#### 2026-08-15 re-survey — new viable orgs

Sources: full upcoming live-schedule walk (current FP streaming partners) +
vesper probe of 33 org names. All below have real per-fight VOD catalogs.

**WIRED IN 2026-08-15** — all 7 active partners below added to
`SHERDOG_ORGS` (records backfilled: 548 events / 5,440 fights) and to
`_EVENT_PREFIX` in the Fight Pass scraper; `--search-events` run for video
URLs. ZFN turned out to be on Sherdog after all (`ZFight-Night-18328`).

Active FP partners (live-streamed; added as `active: True`):
- **Shooto Brazil** — Sherdog `Shooto-Brazil-13715` (138 events, ~1,377
  fights); clips through Shooto Brazil 138; #139 live 2026-08-28. Biggest
  catch. Distinct from the Japanese Shooto already in DB.
- **BFL / Battlefield Fight League** — Sherdog covered; Canada's largest
  promotion; per-fight clips (BFL 87) AND full-event replays (BFL 66–71);
  BFL 88 live 2026-08-28.
- **Tuff-N-Uff** — Sherdog covered; Vegas; long-time FP partner; clips
  through 155; #156 live 2026-08-20. Mixed amateur/pro — is_amateur matters.
- **Unified MMA** — Sherdog covered; Edmonton; clips through Unified 62.
- **INKA MMA** — Sherdog `Inka-MMA-1673` (59 events, ~327 fights); Peru;
  clips through Inka 59; #60 live 2026-08-20 (Spanish + English feeds).
- **Bison Kombat** — Sherdog `Bison-Kombat-10127` (8 events); Brazil; clips
  through Bison Kombat 8; #9 live 2026-08-21. Small but growing.
- **ZFN** — Korean Zombie's Korean org; clips (ZFN Origin 04); #05 live
  2026-08-22. Sherdog `ZFight-Night-18328` (found on second look — an
  earlier search missed it).

**WIRED IN 2026-08-15 (second wave)** — the three "defunct" survey orgs all
turned out to be still running (FAC 36 upcoming; WXC 99 Dec 2025; AFC 175 in
2026), so they were added as `active: True` (241 events / 2,083 fights
backfilled; FP replays + clips matched, incl. `_EVENT_TITLE_ALIASES` /
`_QUERY_EXPANSIONS` bridging the FAC↔Fighting Alliance Championship and
AFC↔Alaska Fighting Championship naming split):
- **FAC / Fighting Alliance Championship** — Sherdog
  `Fighting-Alliance-Championship-15429`; KC; full-event replays + clips.
- **WXC / Warrior Xtreme Cagefighting** — Sherdog
  `Warrior-Xtreme-Cagefighting-6015`; Michigan; event replays + clips
  (early Khaos Williams / Parisian).
- **Alaska FC** — Sherdog `Alaska-Fighting-Championship-492`, 186 events;
  event replays + clips for the 149–157 era.

Re-confirmed NOT on FP: KSW, Oktagon, BAMMA, M-1, Combate Global, Sengoku,
DREAM, DEEP (namesakes only). Grappling-only (out of scope): Submission
Hunter Pro, UFC BJJ.

#### 2026-08-15 playlist discovery — org "Event Replays" playlists

Full-card replays for regional orgs also live in dedicated playlists
(`/video/<id>?playlistId=<pid>`) that appear in NO browse bucket and are NOT
search-indexed. Found by scanning playlist ids 21500–23600 after the user
surfaced Tuff-N-Uff's (22705). `REPLAY_PLAYLISTS` in `fightpass_scraper.py`
now walks: 22701 A-1 Combat (32 cards), 22702 Anthony Pettis FC (26, incl.
the APFC: IFL cards), 22705 Tuff-N-Uff (29), 22706 Unified MMA (25).

**WIRED IN 2026-08-15 (third wave)** — the four playlist-discovered orgs,
added to `SHERDOG_ORGS` (113 events / 1,565 fights backfilled), the
amateur-flagging list, `_EVENT_PREFIX`, and `REPLAY_PLAYLISTS`:
- **Combat FC** (Combat Fighting Championship, Massachusetts) — Sherdog
  `Combat-FC-1118`; playlist 22703, 11 replays.
- **FCC** (Full Contact Contender, Bolton, England) — Sherdog
  `Full-Contact-Contender-4839`; playlist 22704, 15 replays.
- **Shuriken Fight Series** (Auckland, NZ) — Sherdog
  `Shuriken-Fight-Series-16277`; playlist 22753, 8 replays.
- **Fight Club Rush** (Västerås, Sweden) — Sherdog `Fight-Club-Rush-13287`;
  playlist 21752, 22 replays.

Wider scan done 2026-08-23 (10000–21500, 23600–31000; 2,016 playlists).

**2026-09-07 — low-id replay playlists surfaced by vesper.** Searching
vesper for `'<Org> Event Replays'` returns PLAYLIST cards that the id scans
above never reached (ids < 10000). Added to `REPLAY_PLAYLISTS`: 2620 LUX
Fight League (85 replays; LUX had 0/67 event URLs because it was only ever
reached as a PASS 5 side effect), 1633 Cage Warriors (242), 2543 LFA (216),
2544 Cage Fury FC (89), 9823 Ares FC (32). Reminder: the Fight Pass scraper
is manual and local — a dry run writes `fightpass_matches.txt` only; nothing
reaches the DB without `--push`.
Wired: 27884 ZFN Event Replays, 18836 UAE Warriors Event Replays. New-org
candidates with real replay archives (next wave; check Sherdog coverage):
- **RFA** (28427, Resurrection Fighting Alliance — LFA's predecessor)
- **Fusion Fighting Championship** (18148, Mexico; Spanish-titled VODs)
- **Cage Fight Series** (27882), **WOW FC** (23771, Spain),
  **Samourai MMA** (27880, Quebec), **Medusa** (27903, women's league)
Skipped as non-MMA: Karate Combat, Budo Striking/Sento, PGF, BJJ/grappling
playlists, Hollywood Fight Nights (boxing). UFC 2022/2023/2024 Event
Replays playlists exist (16220/20627/20628) but the series walk already
covers those years.

#### Already in DB
| Org | Sherdog ID | Fight Pass | Active |
|---|---|---|---|
| UFC | 2 (via ufcstats) | series 2284 + HCpi + 9bTN + vesper | ✓ |
| Road to UFC | (under UFC, slug-filtered) | series 2924 | ✓ |
| PRIDE | 3 | uj-A + ioe3 + Md4s | — |
| Strikeforce | 716 | 97Ms + JW-A | — |
| WEC | 48 | _7Cg + cbRY | — |
| Bellator | 1960 | — (DAZN/PFL now) | — |
| WSOF | 5449 | — | — |
| PFL | 12241 | — (own platform) | ✓ |
| MVP | 19337 | — | ✓ |

#### Tier 1 — small / defunct + clear Fight Pass + Sherdog match
*(One-shot scrape; predictable parse; immediate per-fight URL coverage via vesper)*
- **Affliction** — only 2 events ever ("Banned" 2008, "Day of Reckoning" 2009);
  vesper finds 17 per-fight VODs. Sherdog ID TBD (probe `Affliction-Banned`
  event page during impl).
- **EliteXC** — Sherdog 937; defunct 2008; ~30 events; 19 per-fight VODs on FP.
- **Vale Tudo Japan (VTJ)** — Sherdog 12; classic Japanese MMA; 20 per-fight
  VODs on FP under "Vale Tudo Japan N" naming.
- **King of the Cage** — Sherdog 1; long history; 20 per-fight VODs on FP.
- ~~**BodogFIGHT** — Sherdog 883; defunct ~2008; ~11 VODs on FP.~~ *(2026-07-08:
  those VODs are one HookNShoot tournament, not BodogFIGHT — skipped.)*

#### Tier 2 — active regional feeders + clear FP content
*(Recurring scrape; active=True; cleanest Fight Pass content)*
- **Cage Warriors** — Sherdog 186; UK/Europe; very active; 20 per-fight VODs
  in classic "X vs Y Cage Warriors N" format. **High value** — McGregor,
  Edwards, Pimblett all came from here.
- **Invicta FC** — Sherdog ID TBD (probe one of their events); women's MMA;
  ~50 events; mixed event-replay + per-fight VODs on FP. Important for
  women's coverage.
- **LFA (Legacy Fighting Alliance)** — Sherdog ID TBD; US feeder; 20 per-fight
  VODs in "X vs Y LFA N" format.
- **CFFC (Cage Fury FC)** — Sherdog ID TBD; US east coast; 20 per-fight VODs.
- **Fury FC** — Sherdog ID TBD; US; 20 per-fight VODs.
- **Pancrase** — Sherdog 19; Japan, 60+ year history; modern era VODs on FP
  in "X vs Y Pancrase NNN" format.
- **Shooto** — Sherdog 21; mainly Shooto Brazil VODs on FP.

#### DONE — 2026-07-08 expansion wave (12 orgs)
Re-probed the candidates against live vesper and added the confirmed ones to
`SHERDOG_ORGS` + `_EVENT_PREFIX`:
- **Active:** UAE Warriors (5307), Eternal MMA (5231), Ares FC (15823),
  Urijah Faber's A1 Combat (17054), UWC Mexico (2611, labeled `UWC`),
  LUX Fight League (12569, labeled `LUX`), JCK / Jue Cheng King (16251),
  APFC (16795), XFN / Xtreme Fighting League Oklahoma (575)
- **Defunct archives:** Legacy FC Texas (2602), CES MMA (3124),
  iKON FC (16451 — covers both the Masvidal iFC and earlier iFF eras)

Name normalization happens in `name_clean` so Fight Pass hints match:
"XFL - Xtreme Fight Night 409" → "XFN 409", "LUX 058" → "LUX 58",
"UWC Mexico 58" → "UWC 58", "LFC 63 - …" → "Legacy FC 63".

Deferred (real FP content, marginal value): Gladiator Challenge (~700 events,
legacy VODs only), HookNShoot (single Bodog tournament), Battlefield Fight
League, Urban Warfare.

#### DONE — 2026-07-10 YouTube-video wave (2 orgs)
Orgs with **no Fight Pass content but a reliable official YouTube channel** —
wired to `SHERDOG_ORGS` (records) + `CHANNELS` in `youtube_scraper.py` (video),
not Fight Pass. Both `active: True`:
- **ONE Championship** (Sherdog `One-Championship-3877`, 400+ events) — slug key
  `one-championship-`, name strips "One Championship - " prefix. YouTube channel
  `UCiormkBf3jm6mfb7k0yPbKA`; full fights in event-named "... Full Fights"
  playlists. MMA-only (Sherdog skips ONE's Muay Thai/kickboxing/grappling).
- **Brave CF** (Sherdog `Brave-Combat-Federation-11227`, ~107 events) — slug key
  `brave-cf-`, name strips "- Brave Combat Federation ..." suffix. YouTube
  channel `UCos8j6dB44G_nlNpdJdP8SQ`; "FREE FULL FIGHTS" + per-event playlists.

Run once after deploy: `python sherdog_scraper.py --all` (backfill records) then
`python youtube_scraper.py --push` (attach videos). Note: ONE/Brave post
per-fight full fights only on YouTube — their full events are paywalled
(Prime Video / BRAVE TV), so no event-level YouTube URLs for them today. The
full-event matcher is now org-agnostic (see Video sources) and will pick them
up automatically if that ever changes.

#### Tier 3 — resolved 2026-07-08
- **Titan FC** — done earlier (Sherdog 740).
- **XFN**, **JCK** — done (see above).
- ~~**DEEP**~~ — **not on Fight Pass** (vesper only returns namesakes:
  Gladiator Challenge "Deep Impact", QUINTET, APFC India). Records-only
  candidate for a later Sherdog-only add.
- ~~**M-1 Global**~~ — **not on Fight Pass** (vesper noise only).
- ~~**Combate Global**~~ — **not on Fight Pass** (vesper returns A-1 Combat).

#### Skip — confirmed not on Fight Pass
*(Vesper returns 0 or only noise. Content lives elsewhere; revisit if scope changes.)*
- **KSW** (Polish — DAZN) — vesper 0 results
- **RIZIN** (Japanese — own platform) — vesper 0 results; video via YouTube instead
- **BAMMA** (defunct) — vesper 0 results
- **ONE Championship** (own streaming) — vesper hits only namesakes; **added
  2026-07-10 via YouTube instead** (see YouTube-video wave above)
- **Bellator** (PFL/DAZN now) — vesper hits only namesakes
- **Sengoku, DREAM, IFL** — vesper hits unrelated content
- **DEEP, M-1 Global, Combate Global** — re-probed 2026-07-08, noise only
- **BodogFIGHT** — the "~11 Bodog VODs" are actually one HookNShoot
  tournament, not the BodogFIGHT org; nothing of BodogFIGHT itself on FP

#### Per-org implementation checklist
For each org we add:
1. **Sherdog config** — append `SHERDOG_ORGS` entry: `org_url`, `slug_keys`,
   `name_clean`, **unique `fight_prefix`** (never reuse, never change once
   data exists), `active` flag. Test parse one event before committing.
2. **Sherdog backfill** — `python sherdog_scraper.py --all` (defunct) or daily
   incremental (active).
3. **Fight Pass event-level URLs** — if the org has a dedicated
   `VOD_PLAYLIST level=event` bucket (PRIDE/SF/WEC pattern), add to
   `PLAYLIST_BUCKETS`. Most Tier 2/3 orgs don't have one — skip.
   Confirmed 2026-08-06: regional-org full-event replays DO exist but are
   **completely unindexed** — not in vesper search, not in any browse bucket,
   isolated in the VOD-id space, titled like the event (e.g. vod 985090
   'Fury FC 122', 2.5 h). The only way to find them is the `--scan-vods`
   id scan, which now also event-matches such titles (guarded by a
   description-date check ±3 days and a ≥1 h duration floor). Some orgs
   (KOTC, Eternal MMA, UAE Warriors, Invicta, CES, Cage Warriors) also have
   vesper-indexed replays that `--search-events` finds directly.
4. **Fight Pass per-fight URLs** — extend `_EVENT_PREFIX` regex in
   `parse_fight_vod_title()` with the org's keyword(s). Currently:
   `(?:UFC|Strikeforce|PRIDE|WEC|Bellator|Noche\s+UFC)`. Need to add (in
   roughly the order they're implemented):
   - `Cage\s+Warriors`, `Invicta(\s+FC)?`, `LFA`, `CFFC`, `Fury\s+FC`,
     `Pancrase`, `Shooto`, `KOTC|King\s+of\s+the\s+Cage`, `Affliction`,
     `EliteXC`, `Vale\s+Tudo\s+Japan|VTJ`, `DEEP`, `Titan\s+FC`, `XFN`,
     `M-1`, `JCK`, `Bodog`.
   Order matters: longer phrases first to avoid premature match.
5. **Vesper search** — `--search-vesper` already catches per-fight VODs once
   fighters/fights are in DB. Run it once after the Sherdog backfill.
   For **recent-event gaps** prefer `--search-events` (PASS 5, added
   2026-08-06): queries vesper by event name instead of fighter last name, so
   a whole card's clips arrive in one query instead of drowning below the
   ~20-result cap on common surnames ('King', 'Garcia'). Covers per-fight
   gaps for events since `--since` (default 2025-01-01) plus event-level
   replay gaps for UFC/PRIDE/Strikeforce/WEC at any date.
   `--events <ids>` (added 2026-08-15) scopes PASS 5 to named event ids at any
   date and skips the series/bucket walks — a targeted backfill of a few
   known-incomplete cards without a full catalog crawl. Note the pass queries
   vesper by *Sherdog* event name, and Fight Pass doesn't use Sherdog's Fight
   Night numbering, so on older cards only the `A vs B` headliner query bites;
   for a specific missing bout, searching the fighters' surnames directly is
   still the surer probe.
6. **Brute-force scan** — `--scan-vods` covers the legacy 25k–60k VOD range.
   Already-scanned IDs are skipped via `fightpass_scan_progress.txt`.
   Re-run with a higher `--scan-end` (existing default is 60000; new VODs
   are pushing past 970000 now) — but only when chasing the long tail.

#### Risks
- **Fighter name encoding** — DEEP/Pancrase/JCK use kanji/hanzi names that
  Sherdog romanizes inconsistently. Expect alias work in `fighter_aliases`.
- **Weight class formats** — old/foreign orgs sometimes used non-standard
  weight categories. Schema is text-typed so it tolerates this, but UI
  filtering may need new buckets.
- **Duplicate fighter records** — fighters who span orgs (e.g. a LFA fighter
  who later signs with UFC) get name-hash UUIDs in both. The Sherdog-ID
  migration (Phase 1 of north-star) is the proper fix; until then expect
  the orphan-cleanup pattern we used for "Yi Sak Lee".
- **Catchweight / open-weight** — PRIDE/early UFC fights need the
  `catch_weight_lbs` column from the target schema; current DB doesn't have it.
- ~~Fighter pages have no photo or link out to Sherdog.~~ **DONE 2026-09-05:**
  `migrate/add_fighter_profile.sql` added `fighters.image_url / nationality /
  date_of_birth / height / association / profile_scraped_at`, filled by the new
  `fighter_profile_scraper.py` (crawls `/fighter/<id>`; resumes off
  `profile_scraped_at`, wired into the nightly at `--limit 2000`). The app's
  fighter page renders the portrait, a bio chip row and a Sherdog link — the
  link needs no stored column since `fighters.id` *is* the Sherdog id and
  `/fighter/<id>` redirects to the canonical slug. Photos are hotlinked with
  `referrerpolicy="no-referrer"` (Sherdog 403s a foreign `Referer`); mirroring
  38k portraits into Supabase Storage would be ~1.4 GB, over the 1 GB free tier.
  Sherdog has no photo for a large share of regional fighters — those render as
  initials.

## Rankings

### DONE — Rankings page: UFC + Sherdog  (added 2026-09-06)
- `rankings` table (`migrate/add_rankings.sql`) + `rankings_scraper.py` in the
  nightly. UFC: champion + top 15 per division and both P4P lists off
  ufc.com/rankings, names resolved to `fighters.id`. Sherdog: its multi-page
  rankings article + the two P4P articles (exact fighter ids). The table is a
  **history of dated snapshots**: a run stores a new `snapshot_date` set only
  when a source's lists changed, and never touches earlier ones.
- App side (Rewatch repo): new **Rankings** nav tab (`js/rankings.js`) — UFC /
  Sherdog toggle, one card per division, champion on top, movement arrows,
  Sherdog record + promotion, click-through to the fighter page; entries with
  no `fighter_id` link out to the source instead. An "as of" date picker
  (from the `rankings_snapshots` view) opens any earlier snapshot.
- Unmatched UFC names land in `rankings_unmatched.txt` — fix with
  `add_alias.py`. Nine aliases were added up front (Loopy Godinez, Bia
  Mesquita, Ian Machado Garry, Michael Venom Page, Khalil Rountree Jr.,
  Benoît Saint Denis, Jan Błachowicz, Ramazan Temirov).
- **Fight rank tags (2026-09-06).** The same run stamps `fights.fighter1_rank`
  / `fighter2_rank` on upcoming UFC fights from the latest UFC snapshot ('C',
  '1'..'15'); past fights are never touched, so the tag freezes at what the
  fighter carried into the bout. The app shows it as a small raised number
  ahead of the name (college-football style), gold `C` for champions. UFC
  fights only for now.
- **Localized ufc.com pages (hit 2026-09-07).** The first nightly run got a
  partly Spanish rankings page from the GitHub runner ("Peso pluma", "Gallo de
  las mujeres") and stored it as a snapshot under those names. Fixed: the
  request forces English (`?language_content_entity=en` + Accept-Language),
  known Spanish/Portuguese headers translate back via `UFC_DIVISION_ALIASES`,
  and any header still outside `UFC_DIVISIONS` aborts the UFC snapshot. If the
  nightly ever logs "unrecognized division headers", add the strings to the
  alias map. The bad snapshot was deleted.

### TODO — historical fight ranks
- Backfill `fighter1_rank` / `fighter2_rank` for past fights. Sherdog keeps a
  monthly rankings archive (`/monthly-archive/rankings/YYYYMM`, articles back
  years), so each past fight can be tagged with the Sherdog rank in force at
  the time. Decide first whether past UFC fights should carry the *UFC* rank
  (Wayback Machine copies of ufc.com/rankings) or the Sherdog rank — the two
  disagree, and today's upcoming-fight tags are UFC ranks.
- Non-UFC upcoming fights (PFL, ONE, RIZIN…) could carry the Sherdog top-10
  rank the same way; needs a per-fight source marker so a `#5` on a PFL card
  isn't read as a UFC rank.

### TODO — more ranking sources  (internet pass 2026-09-08)
Probed every candidate with plain `requests`-style fetches (browser UA, no JS).
- **Fight Matrix** (fightmatrix.com) — the one to add next. Algorithmic
  (CIRRS / ELO variants), all orgs, men + women, P4P. Plain server-rendered
  HTML, robots.txt allows everything, no Cloudflare. `/mma-ranks/<division>/`
  is 25 rows a page (LW runs to 59 pages — take the first 4 = top 100).
  Each row: rank, movement, name, age, record, points, last/next fight.
  Fighter profiles (`/fighter-profile/<Name>/<id>/`) link the fighter's
  **Sherdog page**, so ids resolve exactly — no name matching needed (cache
  the fm_id → sherdog_id map; one profile fetch per new fighter). Monthly
  **archive back to 2008** at `/historical-mma-rankings/ranking-snapshots/
  ?Issue=<n>&Division=<code>` (245 issues; Division codes 1..17, -1/-2 for
  P4P) — the natural source for the historical fight-rank backfill above.
  Their data is also sold via the PolyData API (partnership Mar 2026) —
  not needed while the pages stay open.
- **Tapology** — list pages (`/rankings/ufc`, `/rankings/<id>-<slug>`) return
  200 from a home IP with a browser UA, but bout/fighter pages sit behind a
  Cloudflare "Just a moment" challenge (403), and robots.txt carries an
  ai-train=no content signal. The fan-voted lists only use the **100–150
  most-recent votes**, so they are noisy. Its algorithmic UFC rankings are a
  possible third `source`; the fan lists are not worth the trouble. Expect
  the GitHub runner to be blocked — would need a fallback like Sherdog's.
- **Verdict MMA** `/fighter-rankings` is just the official UFC list — skip.
- **MMA.SOCIAL** (fan-rated fighter skills + ELO) — 1–7 ratings per entry,
  1–3 per fight. Too thin. Good Fights — ratings need sign-in, not in HTML.
  MMA Bridge — event-level only, client-rendered.

### DONE — crowd fight ratings as "other users"  (planned 2026-09-08, built 2026-09-16)
Built as planned below: `migrate/add_crowd_ratings.sql` (tables
`fight_crowd_ratings`, `event_crowd_ratings`, `fight_bonuses`; `fight_search`
+ `crowd_rating` / `crowd_rating_count` / `crowd_source` / `bonus_awards`),
`verdict_scraper.py` and `bonus_scraper.py` in the nightly, and in the app
(Rewatch `js/events.js`) a `blendRating()` helper — crowd score halved to
stars, weight `min(1, count/50)` against your rating, +0.25 star for FOTN /
+0.1 for the per-fighter bonuses — rendered as a quiet grey number after the
stars with the detail in its tooltip; the bonus name is muted italic text in
the sub-meta line. `node tests/crowd-blend.cjs` covers the rule. Back-catalog:
Verdict `--sweep 1-2000` and Wikipedia `--all` were run once on 2026-09-16.
Still open: show `event_crowd_ratings` (event score + hype) on the event
header; Verdict's non-UFC name spellings (RIZIN, ONE) land in
`verdict_unmatched.txt` — add aliases as they appear.

Original plan (kept for the reasoning):
Goal: show a community score next to your own rating, and let a blended
"consensus" weight it in. **Source: Verdict MMA** (verdictmma.com) — the only
site with real per-FIGHT volume:
- `/event/<id>` is server-rendered (Nuxt SSR, 1.4 MB): `Event Rating x/10
  (n)`, `Hype Rating x/10 (n)`, then one `Fight Rating` line per bout —
  "Umar Nurmagomedov vs. Song Yadong 8.36 /10 (444 ratings)". UFC 330 main
  event had 887 ratings; DWCS bouts 60–110; PFL / RIZIN / ONE cards exist.
- Coverage: every UFC event since UFC 1 has a page (ids are NOT
  chronological — 800 = UFC 32, 1800 = TUF 6 Finale), but fight ratings
  only carry volume from **~late 2025 on** (Mar 2025 card: 4 ratings; Sep
  2025 DWCS: 1–2; 2026 cards: hundreds). So it is a going-forward source
  plus a one-time sweep of 2025–26 — not a back-catalog.
- Access: HTML pages are open (robots.txt only disallows `/api/`; the
  `/api/v1/bff/...` JSON needs auth — don't use it). Terms page has no
  scraping clause. Nightly: fetch the recent-events index (`/events/recent`),
  then each event page from the last ~14 days; re-fetch for ~30 days so
  counts settle. Match bouts to `fights` by event date + fighter names (same
  alias machinery as the video scrapers), store unmatched to a review file.
- Schema sketch: `fight_crowd_ratings (fight_id FK, source text, rating
  numeric(4,2) on a 0–10 scale, rating_count int, hype numeric, hype_count,
  source_url, scraped_at, unique(fight_id, source))` + `event_crowd_ratings`
  for the event/hype scores. Keep it separate from `ratings` (personal,
  irreplaceable). Expose via `fight_search` as `crowd_rating`,
  `crowd_rating_count`.
- Blend: `consensus = (my_rating*W_me + crowd_avg*W_crowd) / (W_me+W_crowd)`
  with `W_crowd = min(1, count/50)` (or Bayesian: prior = source mean). Your
  ratings are numeric(3,1) — treat both as 0–10. App shows the crowd score as
  a quiet secondary number, not a pill (see UI rule).
- Second source for the back-catalog: **UFC bonus awards** (Fight / Performance
  of the Night) from Wikipedia event pages ("Bonus awards" bulleted section) —
  objective, covers 2005→, cheap to scrape. Could feed a small fixed bump.
- Tapology's annual "Fights of the Year" lists (fan-voted, 2010→) give a
  top-50 per year for the back-catalog, but the vote pool is tiny (100 most
  recent) — low weight if used at all.

## Video sources
- ~~Generalize the YouTube scraper's channels beyond the PFL channel.~~ **DONE
  2026-07-10:** `CHANNELS` in `youtube_scraper.py` now covers PFL (+Bellator/
  WSOF), RIZIN, ONE Championship, and Brave CF. Adding an org's channel = one
  `CHANNELS` entry — nothing else to configure.
- ~~Full-event matching is PFL-only.~~ **DONE 2026-07-10:** `match_full_event`
  is now org-agnostic (no promotion name hard-coded) and runs for every channel;
  it also fixed a latent PFL bug — the old `^YYYY PFL <key>` regex silently
  dropped PFL's newer regional names ("2026 MENA: PFL Riyadh", "2026 PFL Africa:
  Nigeria"). Highlights/recaps/promos are rejected (`_NOT_FULL_EVENT`). Roman
  numerals (II–X) fold to arabic and an exact name-token tier splits themed
  siblings ("Fists of Fury" vs "Fists of Fury 2"). Full-event videos embedded
  inside per-fight playlists are now routed to the event matcher instead of
  PARSE_FAILing (`_is_full_event_title`), plus the bounded uploads scan.
- **ONE full events ARE on YouTube but US-geo-blocked (2026-07-10).** ONE files
  themed full-event replays inside its 'Full Fights' playlists (Fists of Fury,
  Unbreakable, Collision Course, Big Bang, Inside the Matrix — 14 videos). The
  matcher now resolves all 14 to the exact event, but ONE sets
  `regionRestriction.blocked = [AT,CH,DE,IN,JP,NZ,US]`, so with `COUNTRY='US'`
  they're matched then dropped — 0 stored for a US audience. (8 of PFL's newer
  full events are US-blocked the same way.) This is the free-YouTube answer to
  the skipped Prime Video crawler: the content exists, but ONE blocks the US.
- ~~Rematch VODs land on the wrong bout.~~ **DONE 2026-08-15:** a per-fight VOD
  title's event hint is usually generic ("Amanda Ribas vs Mackenzie Dern UFC
  Fight Night"), and it was substring-tested against each candidate event with
  first-hit-wins — but *every* UFC rematch's event name contains "UFC Fight
  Night", so the pick was effectively arbitrary and fight 1's video regularly
  landed on the rematch. `pick_by_hint()` now requires the hint to single one
  candidate out, else the VOD is reported unmatched instead of guessed at (all
  three pick sites: bucket walk, vesper search, id scan). Pass 3 also resolves a
  *specific* hint against all events before the "only one candidate" shortcut —
  its candidates are filtered to the bucket's orgs, so a cross-org rematch
  ("... Strikeforce: Rousey vs Kaufman" walked under a UFC bucket) left the
  wrong bout as the sole candidate. 10 mis-assigned URLs repaired in the DB
  (Dern/Ribas, McCann/Belbita, Nascimento/Mayes, Vera/Grant, Ngannou/Blaydes,
  Edgar/Swanson, Davis/Carmouche, Benavidez/Formiga, Jacare/Brunson ×2). The
  counterpart bouts were then backfilled by probing vesper per fighter pair:
  6 filled, 2 genuinely have no per-fight VOD on Fight Pass (Ngannou/Blaydes 2
  — full-event replay only, already on `events.fightpass_url`; Vera/Grant 2 —
  absent from Marlon Vera's whole FP catalog).
- ~~FCR 27 Berisha vs da Silva Marques had no per-fight URL.~~ **DONE
  2026-09-06:** Fight Pass abbreviates some orgs in per-fight clip titles
  ("Arlind Berisha vs Wesley Silva FCR 27") while the DB spells them out
  ("Fight Club Rush 27"). Exact-name matching still worked for the rest of the
  card, but any bout with Fight Pass name drift ("Wesley Silva" for "Wesley da
  Silva Marques") depends on `event_anchored_fight()` pinning the card from the
  title hint, and "FCR 27" shared no tokens with the DB name. `_HINT_ALIASES` +
  `expand_hint()` now spell out FCR/SFS/BFL/APFC/VTJ before every hint-based
  event match (`event_anchored_fight`, `pick_by_hint`, the bucket-walk hint
  path). `_QUERY_EXPANSIONS` gained the reverse direction so `--search-events`
  also queries vesper by the abbreviation — "Fight Club Rush 27" alone only
  surfaced the full-card replay. Backfilled 3 fights (FCR 27 Berisha/Silva
  Marques, FCR 29 Al-Jasim/Silva do Nascimento, FCR 29 Kveldstadt/Grabowski).
- **DWCS per-fight VODs — two seasons have none anywhere (checked 2026-09-09).**
  Season 7 / 2023 (10 cards, 49 fights): Fight Pass only carries the full
  "Season 7, Week N" episodes — vesper searches for a dozen of that season's
  fighters return zero Contender Series clips — and Paramount+ season 7 is 10
  "Full Episode" items. Season 10 / 2026: not on Fight Pass at all (series
  2285 lists "DWCS Season 10" but `/api/v4/season/36736` 404s, no 2026 fighter
  has a clip); Paramount+ season 10 is "Week N" full episodes only, already on
  `events.paramount_url`. So those fights correctly show just the event replay.
  Per-fight clips do exist for 2017–2022 ("… Dana White's Contender Series")
  and 2024+ ("… DWCS"); the leftover gaps there were name-drift cases that had
  never been through the date-anchored fallback (added for the 2025 season).
  `--search-events --events <37 DWCS card ids>` backfilled 17 of them
  (S1 ×4, S2 ×2, 2021 ×7, 2022 ×3, 2024 ×1) plus 23 incidental regional-org
  bouts and 3 UWC event replays surfaced by the same surname queries.
- ~~LFA's YouTube channel (@lfafights) barely matched anything.~~ **DONE
  2026-09-06:** the channel was wired but only 16 of 2,658 LFA fights and 9
  events had links, because (a) the playlist scan only takes playlists titled
  "full fight" and LFA files its archive under 'BEFORE THE UFC', 'WOMEN OF
  MMA', 'Best of LFA', 'LFA Classics' and the full cards under 'LFA FULL
  EVENTS', 'LFA Main Cards 2026' and a prelim-replay list; (b) full-card titles
  are buried in filler ('LFA 241 Main Card *LIVE* | 6 x Fight Full Card') that
  the token tiers couldn't see through; (c) prelim replays carry no 'full'
  marker at all. Fixes in `youtube_scraper.py`: per-channel `fight_playlists`
  (extra per-fight lists) and `card_playlists` (id → default card half; every
  video is a full-card candidate, gated by `MIN_FULL_CARD_SECS` = 40 min via
  one videos.list call); a tier-0 *numbered event* matcher ('<Org> <N>' unique
  in scope) with `_TITLE_ORG_ALIASES` folding 'Legacy Fighting Championship'
  → 'Legacy FC' and 'LFA MMA 3' → 'LFA 3'; the reverse-subset tier now needs
  the org in the title when the event name is bare '<Org> <N>' (RFA 33 and
  Attitude MMA 31 re-airs were landing on Legacy FC 33/31); weigh-ins /
  pressers rejected by title; 'Preliminary' counts as prelims; scheduled live
  streams for future-dated events are skipped until the card happens; a
  matched per-fight video ≥ 40 min is dropped as a card stream; highlight
  clips are rejected before the A-vs-B parse. The channel now also covers
  Legacy FC (its re-aired predecessor; RFA isn't in the DB, so those stay
  unmatched). LFA-only push: 119 fights, 84 event entries (26 main + 58
  prelim), 9 recent main cards matched but US geo-blocked.
- KSW/Oktagon remain unwired (paywalled — no free channel).
- **Prime Video / watch.onefc.com — evaluated & skipped (2026-07-10).** ONE's
  full-event replays live on Amazon Prime Video (US-only, subscription) and
  ONE's own OTT. Neither is reliably crawlable: the Amazon ONE tournament page
  server-rendered event titles once, then returned a stripped JS-only shell on
  0/5 retries (fingerprints + throttles bots; catalog is session-gated ajax);
  watch.onefc.com is a Next.js app whose SSR carries no catalog — content sits
  behind an auth-gated client API. Both would store paywalled, geo-locked links
  (lower value than the free ONE YouTube per-fight coverage already wired). Not
  worth a crawler; revisit only if a public/free endpoint appears.
- **Pluto TV — Bellator replays, DONE 2026-09-06 (one-off backfill).** Pluto
  (free, ad-supported, Paramount-owned, US region) carries two Bellator series:
  "Bellator MMA Full Fight Cards" (136 full-event replays, Bellator 58–301 +
  Bellator NYC) and "Bellator MMA Individual Fights" (206 single-bout VODs,
  Bellator 140–239). `migrate/add_pluto_url.sql` added `pluto_url` to `events`
  + `fights` (+ `fight_search`); `pluto_scraper.py` pulls both series from the
  unauthenticated boot endpoint (one call, no paging) and matches by **event
  number** (Pluto's air dates are TV dates, months off) — per-fight bouts are
  anchored to the card by the Pluto season number, so surname / nickname /
  fuzzy-spelling matches are safe. Result: **135/136 events, 200/206 fights**
  stored. Leftovers, all expected: Bellator 180 "Davis vs. Bader" is the same
  MSG night as Bellator NYC (Sherdog has one event; NYC's PPV card wins), 3
  Bellator Kickboxing bouts (not MMA), 2 Pluto mislabels (Salter/van Steenis
  filed under 223 — happened at 233; "Alfie Davis vs. Jorge Kanella" @223 —
  Sherdog has Davis vs. da Paz), and "Marco Hutch" for DiMarcco Hutchison.
  **Not wired into the nightly** — Bellator is defunct, the catalog is static;
  re-run `python pluto_scraper.py --push` by hand if Pluto adds episodes (it
  only writes rows whose URL changed). "Best of Bellator" (compilations) and
  PFL's Pluto channel/timelines (no full events) were skipped.
- **Bellator YouTube channel wired, DONE 2026-09-06.** `CHANNELS` in
  `youtube_scraper.py` now includes BellatorMMA (UCnkMTsKYqhHm6l6GQzg4szg —
  orgs Bellator/PFL/RIZIN): one 1020-video 'Full Fights | Bellator MMA'
  playlist (~150 private) plus per-card 'Bellator 300 | Full Fights' lists and
  'Re-Air' full cards. Titles are hype-first ('CYBORG ARRIVES! | Cris Cyborg v
  Julia Budd | Full Fight | Bellator 238'), which drove four general matcher
  upgrades: (1) per-video event hint from the title's trailing tag
  (`title_event_hint`) with token-wise whole-word matching, so 'bellator 58'
  finds 'BFC - Bellator Fighting Championships 58' but 'bellator 23' can't hit
  238; (2) series ordinal ('… II', '… 4', '… I') picks the Nth meeting by date
  (`rematch_ordinal`); (3) an exact full-name hit that yields no bout widens
  to the surname pool ('Chris Gonzalez' ≠ Bellator's 'Christopher Gonzalez');
  (4) full-event keys try every pipe segment, treat 'Re-Air' as a full-card
  marker, fold ' v ' to 'vs' (was read as roman 5), and add a numbers-equal
  tier plus a reverse-subset tier for re-airs titled with full names. Also a
  spaced-dash segment delimiter ('… Girtz 3 - Bellator 181') and accent
  folding before the non-ASCII strip ('Patrício' was becoming 'Patrcio').
  Nickname aliases added via `add_alias.py`: Patricio/Patricky Pitbull, King
  Mo, Cris Cyborg, Kimbo Slice, Marina Mokhnatkina, John Macapa, + spelling
  variants (Shabliy, Taylor-Melendez, Macfarlane, Yagshimuradov). Bellator
  channel alone: **758/826 fights, 36 events**. Leftovers: kickboxing bouts,
  one-off typos ('Ronnie Man', 'Rene Nazar', 'R. Cavalheiro'), EuroSeries
  re-airs (no Sherdog event of that name), 'Bellator NYC' (Sherdog: 180).
- **Real bug fixed 2026-09-06 — unordered PostgREST paging.** `fetch_all()` in
  `youtube_scraper.py` (and the new `pluto_scraper.py`) paged with
  `limit/offset` and **no `order`**, so rows were skipped/repeated between
  pages — on the 39-page `fighters` table this silently dropped fighters and
  showed up as "fighter not found". With `order=id` (`order=alias` for
  `fighter_aliases`, which has no id) the full YouTube run went from
  868 → **1943 matched fights** and 13 → **49 events** across all channels.
  The same one-line fix was applied to the other nightly scripts' own paging
  helpers — `fightpass_scraper.py`, `gender_scraper.py`,
  `paramount_scraper.py` (`supabase_get`) and the women's-name walk in
  `sherdog_scraper.py` (which feeds the women's-division labelling — a
  skipped page there mislabels divisions). Expect the next nightly Fight Pass
  / gender passes to pick up rows they were silently missing. Still unpatched
  (not nightly): `research_unmatched.py`, `import_ratings.py`, `migrate/*.py`
  — fix before the next use.
- Per source, note free vs. paywalled.

## App performance

### DONE — precomputed read models  (2026-09-16)
- The event page took 3–4 s and the community dashboard 1.6 s, almost all of
  it round trips and server-side joins on the small prod instance. Added
  `migrate/app_perf_precompute.sql`: `event_flags_mv` + `community_fight_base`
  materialized views, `events_index()` / `my_ratings()` RPCs, and
  `community_dashboard()` rewritten over the view. `refresh_app_caches.py`
  rebuilds the views at the end of every scraper pass (workflow step + the
  live-event skill). App side: one request per screen, supabase-js pinned,
  scripts deferred, Chart.js lazy-loaded.
- **Follow-up:** drop `event_result_flags()` once the old app JS has aged out
  of browser caches (it's unused by the current app).
- **Follow-up:** `fight_rating_stats()` (community averages, called on load
  and after every save) still joins `ratings` to the 66k-row `fights` table
  just to exclude amateur bouts — join `community_fight_base` instead.
- **Not done (advisor WARN):** the four `ratings` RLS policies call
  `auth.uid()` per row; wrapping it as `(select auth.uid())` is the standard
  fix. Negligible at 1.3k rows, cheap to do when next touching ratings.

## Ground rules (carried over from prior decisions)
- ~~ufcstats stays the UFC source~~ **SUPERSEDED 2026-06-20:** Sherdog is now the
  single source of truth for every org **including UFC** (`start.py`/ufcstats
  retired as canonical). See `migrate/RUNBOOK.md`. Fighters/events are keyed on
  Sherdog numeric ids; fights on `uuid5(event_id:sorted fighter ids)`.
- Fight ids no longer use per-org `fight_prefix` — the unified Sherdog-id scheme
  replaced it during the source-of-truth migration. (`fight_prefix` remains in
  the org configs only as documentation.)
- Build risky changes on a staging copy of the DB, cut over when proven (see
  "Build approach" above). End state is one DB / one app — not a permanent
  fork. Ratings are irreplaceable: they live in the copy from the start and are
  verified through cutover, never "imported later".
- Non-UFC women's fighters resolve via gender propagation + Wikidata + the
  email review queue.
