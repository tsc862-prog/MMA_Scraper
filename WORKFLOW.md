# Nightly Scraper Workflow

This document explains the GitHub Actions workflow that runs the MMA scrapers on
a schedule. The workflow definition lives in
[.github/workflows/scrape.yml](.github/workflows/scrape.yml).

## What it does

A single job, `scrape`, runs on `ubuntu-latest`. It checks out the repo, installs
Python dependencies, materializes a `.env` from repository secrets, runs a series
of scraper scripts that push data into Supabase, and finally commits any updated
state-tracker files back to `main`.

## When it runs

Three schedule triggers plus manual dispatch (`workflow_dispatch` — the "Run
workflow" button in the Actions tab):

| Cron (UTC) | Meaning | Purpose |
|---|---|---|
| `*/10 22-23 * * 6` | Sat 2–4 pm PST, every 10 min | Live UFC/Sherdog fight-night results |
| `*/10 0-5 * * 0` | Sat 4–10 pm PST, every 10 min | Live UFC/Sherdog fight-night results |
| `30 7 * * *` | 11:30 pm PST daily | Full daily pass (all steps) |

> GitHub's scheduler is best-effort and often delays scheduled runs by minutes
> (the failed `30 7` run actually started at 13:08 UTC). Don't rely on exact
> timing.

A `concurrency` group (`group: scraper`, `cancel-in-progress: false`) ensures two
**workflow runs** never overlap — a new run waits for the in-flight one to finish
rather than cancelling it. (Note: this does **not** protect against a human `git
push` landing on `main` mid-run — see [Gotchas](#gotchas-and-history).)

## Steps

Every run does the Sherdog pass; the rest are gated to the daily run and manual
dispatch via `if: github.event.schedule == '30 7 * * *' || github.event_name == 'workflow_dispatch'`.

| Step | Script | Runs | What it does |
|---|---|---|---|
| Sherdog scraper | `sherdog_scraper.py` | **every** trigger | Source of truth for all orgs incl. UFC. Scrapes events/fights into Supabase, and prunes fights that dropped off a re-scraped card (opponent swaps / cancellations) — fights with results or user ratings are never deleted. (`start.py`/ufcstats is retired.) |
| Resolve genders | `gender_scraper.py` | daily / manual | Resolves fighter gender via Wikidata P21 for unchecked non-UFC fighters; one woman per fight is enough — propagation relabels her opponents' divisions. |
| Propagate genders | `gender_propagate.py --apply` | daily / manual | Cheap, no-API opponent-graph propagation: pro MMA is gender-segregated, so each fight-graph component is single-gender — seed from checked fighters and propagate to unchecked ones, auto-resolving the matching gender review items. Runs after the Wikidata pass so it uses its fresh seeds. |
| Email digest | `review_email.py` | daily / manual | Emails the pending `review_queue` via Gmail SMTP with one-click resolve links (Supabase edge function). |
| ESPN PFL videos | `espn_pfl_scraper.py --push` | daily / manual | Matches PFL event replays from ESPN's Watch API into `events.espn_url`. |
| YouTube videos | `youtube_scraper.py --push` | daily / manual | Matches full-fight/event videos from official promotion channels — PFL (PFL/Bellator/WSOF, full events), RIZIN, ONE Championship, Brave CF, LFA (+ its re-aired Legacy FC cards; since 2026-09-06 the entry names the channel's themed per-fight playlists and its full-card / prelim-replay playlists explicitly, with a 40-min duration gate on full-card candidates), and (since 2026-09-06) the Bellator channel's ~1000-video full-fight archive + 'Re-Air' full cards — into `fights.youtube_url` / `events.youtube_url` / `events.youtube_prelims_url`. Full-card titles are matched first by their '<Org> <N>' number when that is unique in scope. `--channel NAME` scans one channel for testing. |
| Flag amateur fights | `amateur_scraper.py --orgs "…" --limit 2000` | daily / manual | Flags amateur bouts on otherwise-pro regional cards by reading Sherdog fighter pages; incremental via `amateur_checked.txt`. |
| Fighter profiles | `fighter_profile_scraper.py --limit 2000` | daily / manual | Fills `fighters.image_url` / `nationality` / `date_of_birth` / `height` / `association` from Sherdog fighter pages, for the app's fighter page. Incremental via the `fighters.profile_scraped_at` column (not a tracker file), so a nightly run only touches fighters added that day. |
| Rankings | `rankings_scraper.py` | daily / manual | Divisional + pound-for-pound rankings from ufc.com (champion + top 15, names resolved to `fighters.id` via alias / accent-fold / surname-flip / UFC-roster match) and Sherdog (its multi-page rankings article + two P4P articles, exact fighter ids) into the `rankings` table as **dated snapshots** — a source's lists are compared with its latest stored snapshot and stored under today's date only when something changed (older snapshots are never touched); a source that parses to too few rows records nothing. Then stamps every **upcoming UFC fight's** `fighter1_rank` / `fighter2_rank` from the latest UFC snapshot ('C' / '1'..'15' / null); past fights are never touched, so tags freeze at fight-night values. Unmatched names go to `rankings_unmatched.txt` (not committed). Added 2026-09-06. |
| Crowd ratings | `verdict_scraper.py` | daily / manual | Community fight + event scores from Verdict MMA (verdictmma.com — server-rendered event pages; the site's `/api/` is auth-only and not used) into `fight_crowd_ratings` / `event_crowd_ratings` on Verdict's 0–10 scale. Reads the ten cards on `/events/recent` plus every stored event whose date is within 30 days (`--settle`), since ratings keep arriving for weeks. Bouts are matched to the DB by date (±1 day) and fighter names (exact / alias / accent-fold / surname-first flip / surname within that card). No tracker file. Unmatched bouts go to `verdict_unmatched.txt` (not committed). The 2025–26 back-catalog was a one-off `--sweep 1-2000` on 2026-09-16. Added 2026-09-16. |
| UFC bonuses | `bonus_scraper.py` | daily / manual | Fight / Performance (pre-2014: Knockout / Submission) of the Night from the Wikipedia UFC event pages into `fight_bonuses`. Events matched by date from Wikipedia's "List of UFC events" table. Nightly it only fetches UFC events from the last 45 days that have no bonus rows yet (a few pages, 1 req/s, descriptive User-Agent). `--all` backfilled every UFC event on 2026-09-16. Unmatched awards go to `bonus_unmatched.txt` (not committed). Added 2026-09-16. |
| Refresh app caches | `refresh_app_caches.py` | **every** trigger (`if: always()`) | Calls the `refresh_app_caches()` RPC to rebuild the two materialized views the app reads instead of recomputing on every page load — `event_flags_mv` (per-event has-results / has-video flags behind the events list) and `community_fight_base` (per-fight rows behind the community dashboard). Until this runs, the events list and community stats show the previous pass; the event card itself is always live. Runs even if an earlier step failed so whatever did land is visible. The live-event loop (`.claude/skills/current-event`) runs the same script after each pass. Added 2026-09-16. |
| Save trackers | inline git | **every** trigger | Commits updated state files back to `main` (see below). |

## Secrets

The `.env` is built at runtime from these repository secrets:

- `SUPABASE_URL`, `SUPABASE_KEY` — database access
- `YOUTUBE_API_KEY` — YouTube Data API v3
- `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD` — SMTP for the review digest

## State / tracker files

Scrapers persist progress in plain-text files so runs are incremental and
resumable. The workflow commits two of them back to the repo at the end:

- `sherdog_completed_events.txt` — events already fully scraped (skipped next run)
- `amateur_checked.txt` — fighter ids already checked for amateur bouts

Other trackers (`*_matches.txt`, `fightpass_*`, `youtube_*matches.txt`, etc.) are
written for human review and are **not** committed by the workflow.

Not every scraper is in the nightly. `pluto_scraper.py` (Pluto TV's Bellator
replays → `pluto_url`) was a one-off backfill on 2026-09-06 — Bellator is
defunct and the catalog is static, so it's run by hand (`--push`) if Pluto ever
adds episodes, not on a schedule.

`verdict_scraper.py` and `bonus_scraper.py` (added 2026-09-16) also have no
tracker file: the first re-fetches whatever `event_crowd_ratings` already holds
for the last 30 days, the second only looks at recent UFC events with no
`fight_bonuses` rows yet.

`fighter_profile_scraper.py` deliberately has **no** tracker file — it resumes
off `fighters.profile_scraped_at` in the DB. A 38k-id text file rewritten and
committed every night would be pure churn, and it would widen the push race
below.

The final step commits with `[skip ci]` in the message so the bot's own push
doesn't trigger another run.

## Gotchas and history

**Push race on long runs.** The daily run can take **3+ hours** (the Wikidata
gender pass scans every fighter — ~8650 — to confirm a handful of new women). If
anything is pushed to `main` during that window, the bot's checkout is stale and
the final `git push` fails with `! [rejected] main -> main (fetch first)`,
failing the whole run. This is exactly what happened on 2026-06-22.

The "Save trackers" step now defends against this: it commits, then loops up to 5
times doing `git pull --rebase --autostash origin main && git push`, so a
concurrent push no longer fails the run. Because the committed trackers
(`sherdog_completed_events.txt`, `amateur_checked.txt`) are only ever written by
the bot, the rebase doesn't conflict with human pushes to other files.

Potential future improvement: make `gender_scraper.py` incremental (only check
fighters added since the last run, like `amateur_scraper.py` does) to shrink the
3-hour window that creates the race in the first place.

**Unordered paging (fixed 2026-09-06).** Every scraper walks Supabase tables in
1000-row pages via `limit`/`offset`. Without an `order=` PostgREST does not
guarantee a stable row order between pages, so large tables (`fighters` is
~39 pages) silently lost rows — surfacing as "fighter not found" in the video
matchers and, worse, as gaps in the women's-name list `sherdog_scraper.py`
uses for division labels. All nightly scripts now pass `order=id`
(`order=alias` for `fighter_aliases`). If you add a new paging helper, include
an `order=`.

## Operating it

- **Manual run:** Actions tab → *MMA Scraper* → *Run workflow*, or
  `gh workflow run scrape.yml --ref main`. A manual dispatch runs **all** steps.
- **Check status:** `gh run list --workflow scrape.yml`
- **Inspect a failure:** `gh run view <run-id> --log-failed`
