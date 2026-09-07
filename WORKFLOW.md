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
| YouTube videos | `youtube_scraper.py --push` | daily / manual | Matches full-fight/event videos from official promotion channels — PFL (PFL/Bellator/WSOF, full events), RIZIN, ONE Championship, and Brave CF (per-fight) — into `fights.youtube_url` / `events.youtube_url`. |
| Flag amateur fights | `amateur_scraper.py --orgs "…" --limit 2000` | daily / manual | Flags amateur bouts on otherwise-pro regional cards by reading Sherdog fighter pages; incremental via `amateur_checked.txt`. |
| Rankings | `rankings_scraper.py` | daily / manual | Divisional + pound-for-pound rankings from ufc.com (champion + top 15, names resolved to `fighters.id` via alias / accent-fold / surname-flip / UFC-roster match) and Sherdog (its multi-page rankings article + two P4P articles, exact fighter ids) into the `rankings` table as **dated snapshots** — a source's lists are compared with its latest stored snapshot and stored under today's date only when something changed (older snapshots are never touched); a source that parses to too few rows records nothing. Then stamps every **upcoming UFC fight's** `fighter1_rank` / `fighter2_rank` from the latest UFC snapshot ('C' / '1'..'15' / null); past fights are never touched, so tags freeze at fight-night values. Unmatched names go to `rankings_unmatched.txt` (not committed). Added 2026-09-06. |
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

## Operating it

- **Manual run:** Actions tab → *MMA Scraper* → *Run workflow*, or
  `gh workflow run scrape.yml --ref main`. A manual dispatch runs **all** steps.
- **Check status:** `gh run list --workflow scrape.yml`
- **Inspect a failure:** `gh run view <run-id> --log-failed`
