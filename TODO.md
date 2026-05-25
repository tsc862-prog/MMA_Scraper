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

### Other orgs worth evaluating
Once the fighter-page scraper (Phase 2) lands, explicit org configs matter much
less — fighter pages pull in every org anyway. Still useful for events/video:
- **ONE Championship**, **KSW**, **Cage Warriors**, **Invicta FC**, **RIZIN**
  — all on Sherdog; add as `SHERDOG_ORGS` entries if event-level coverage wanted.

## Video sources
- Generalize the YouTube scraper's channels beyond the PFL channel (it already
  covers PFL+Bellator+WSOF; ONE/KSW/etc. have their own channels).
- Per source, note free vs. paywalled.

## Ground rules (carried over from prior decisions)
- ufcstats stays the UFC source — do **not** re-key UFC off another source.
- Never change a Sherdog `fight_prefix` once data exists for that org.
- Build risky changes on a staging copy of the DB, cut over when proven (see
  "Build approach" above). End state is one DB / one app — not a permanent
  fork. Ratings are irreplaceable: they live in the copy from the start and are
  verified through cutover, never "imported later".
- Non-UFC women's fighters resolve via gender propagation + Wikidata + the
  email review queue.
