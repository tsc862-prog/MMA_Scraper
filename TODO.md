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
- **ONE Championship**, **KSW**, **Cage Warriors**, **Invicta FC**, **RIZIN**
  — all on Sherdog; add as `SHERDOG_ORGS` entries if event-level coverage wanted.

### Fight Pass × Sherdog org expansion roadmap

Survey done 2026-06-19 — re-walked the 17 top-level Fight Pass buckets and
vesper-searched 34 org names; cross-referenced with a Sherdog org-ID scan.
Buckets with ≥20 "X vs Y" titles for the org name are real per-fight content;
buckets with 0–4 are noise / namesakes.

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
- **BodogFIGHT** — Sherdog 883; defunct ~2008; ~11 VODs on FP.

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

#### Tier 3 — secondary regionals
*(Worth doing eventually; lower per-event interest)*
- **DEEP** — Sherdog 24; Japan.
- **Titan FC** — Sherdog 740; US.
- **XFN** — Sherdog ID TBD.
- **M-1 Global** — Sherdog 72; Russia; status check needed.
- **JCK** — Chinese org; Sherdog ID TBD; modern Chinese MMA on FP.
- **Combate Global / Combat FC** — needs disambiguation; samples ambiguous.

#### Skip — confirmed not on Fight Pass
*(Vesper returns 0 or only noise. Content lives elsewhere; revisit if scope changes.)*
- **KSW** (Polish — DAZN) — vesper 0 results
- **RIZIN** (Japanese — own platform) — vesper 0 results
- **BAMMA** (defunct) — vesper 0 results
- **ONE Championship** (own streaming) — vesper hits only namesakes
- **Bellator** (PFL/DAZN now) — vesper hits only namesakes
- **Sengoku, DREAM, IFL** — vesper hits unrelated content

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
- Generalize the YouTube scraper's channels beyond the PFL channel (it already
  covers PFL+Bellator+WSOF; ONE/KSW/etc. have their own channels).
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
