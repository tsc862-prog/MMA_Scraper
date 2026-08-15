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

#### 2026-08-15 re-survey — new viable orgs (not yet in DB)

Sources: full upcoming live-schedule walk (current FP streaming partners) +
vesper probe of 33 org names. All below have real per-fight VOD catalogs.

Active FP partners (live-streamed; add as `active: True`):
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
  2026-08-22. **Not found on Sherdog** (Tapology only) — records blocked;
  watchlist until Sherdog picks it up.

Defunct/historic with FP archives (one-shot backfill):
- **FAC / Fighting Alliance Championship** — Sherdog covered; KC (Krause);
  full-event replays (FAC 3–16) + per-fight clips.
- **WXC / Warrior Xtreme Cagefighting** — Sherdog covered; Michigan; event
  replays + 16 clips (early Khaos Williams / Parisian).
- **Alaska Fighting Championship** — Sherdog covered; event replays 155–157
  + clips.

Re-confirmed NOT on FP: KSW, Oktagon, BAMMA, M-1, Combate Global, Sengoku,
DREAM, DEEP (namesakes only). Grappling-only (out of scope): Submission
Hunter Pro, UFC BJJ.

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
- Per source, note free vs. paywalled.

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
