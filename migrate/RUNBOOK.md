# Sherdog source-of-truth migration — runbook

Moves the DB off name-hash/ufcstats ids onto **Sherdog numeric ids** and makes
Sherdog the single source of truth for fighters, cards, and results **including
UFC** (ufcstats / `start.py` retired). The irreplaceable `ratings` table (832
rows) is re-pointed with **zero loss**. All risky work runs on a **staging**
Supabase copy; production keeps running untouched until cutover.

Branch: `sherdog-source-of-truth`. Do **not** merge to `main` until cutover —
`main`'s nightly cron runs the committed scraper against prod.

## Prerequisites (user-provisioned)
- A 2nd Supabase project = **staging** (likely a **paid tier** — a full backfill
  exceeds the 500 MB free tier).
- A staging `.env` with staging `SUPABASE_URL` / `SUPABASE_KEY` (keep prod creds
  separate — the only step that touches prod is the read-only title snapshot).

## One-time, against PROD (read-only)
```
python migrate/ufc_title_snapshot.py     # writes migrate/ufc_title_snapshot.json
```
Safety net for historical UFC `is_title` in case Sherdog misses an old event.

## Build on STAGING
1. `pg_dump` prod → restore into staging (**ratings included**).
2. Point `.env` at staging.
3. `psql … -f migrate/01_prepare.sql`
   — snapshots old data to `*_old`, rebuilds the real tables empty with bigint
   ids, adds `organizations` + `events.organization_id`, retypes `events.date`,
   disables the audit triggers.
4. Sherdog rebuild of the **currently configured orgs** (the full `SHERDOG_ORGS`
   set, incl. UFC — NOT the "every pro event" expansion; new orgs are added later
   once this is stable). Chunked + resumable:
   ```
   rm -f sherdog_completed_events.txt        # fresh rebuild
   python sherdog_scraper.py --all --limit 500   # repeat until no new events
   ```
   `--all` includes the defunct orgs already in prod (Bellator/WSOF/PRIDE/…);
   `--limit` keeps each run inside one job. Re-run until "scraping 0" for every
   org. (Amateur bouts are skipped automatically.)
5. (Optional) apply the title snapshot for any old UFC fight Sherdog left
   unflagged — OR-merge `ufc_title_snapshot.json` into `fights.is_title`.
6. Re-point ratings — **must report 0 unmatched**:
   ```
   python migrate/repoint_ratings.py            # dry run (hard gate)
   python migrate/repoint_ratings.py --apply     # writes ratings.fight_id
   ```
   If any are unmatched: add `fighter_aliases`, fix event names, or hand-map,
   then re-run. Do not proceed until clean.
7. `psql … -f migrate/02_finalize.sql`
   — re-adds FKs (bigint), re-points the ratings FK, recreates `fight_search`,
   re-enables triggers, drops `*_old`.
8. Maintenance RPCs:
   ```
   select delete_orphan_events();
   select recalculate_debut_flags();
   select recalculate_womens_divisions();
   ```
9. `psql … -f migrate/03_verify.sql` — every gate must pass (esp.
   `ratings_orphaned = 0`, ratings_total unchanged, no duplicate fighter names).

## Cutover (planned, with prod frozen briefly)
1. Pause prod writes (disable the GitHub Actions schedule).
2. Re-run the **whole scripted build** on a **fresh** prod copy (steps above).
   Re-copying prod at cutover folds in the small delta of ratings logged during
   the build window **by construction** — nothing is "imported later".
3. Validate (`03_verify.sql`).
4. Repoint the **app** `SUPABASE_URL` to the migrated DB.
5. Merge `sherdog-source-of-truth` to `main` — this activates the retired-`start.py`
   cron (Sherdog incl. UFC). Regenerate the video-scraper `*_matches.txt`
   trackers (they hold stale old fight ids; the scrapers self-heal by name on the
   next `--push`).

## Notes / risks
- **Most likely failure:** UFC ratings (787) not re-matching because Sherdog
  names/events differ from ufcstats. Mitigations: date-first disambiguation in
  `repoint_ratings.py`, an alias pass, and the hard zero-unmatched gate.
- Never map UFC across sources by id — always re-scrape from Sherdog and match by
  natural key.
- `start.py` stays in the repo (retired), only removed from the cron.
