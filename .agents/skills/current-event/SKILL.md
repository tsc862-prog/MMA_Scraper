---
name: current-event
description: Live-event tracking loop — while an MMA event is airing, run the Sherdog scraper (org-scoped) and Paramount scraper every 5 minutes, report per-pass deltas, and auto-stop once the main event's result is in the DB. Use when the user says an event is happening now / tonight and wants results and video links tracked live.
---

# Current-event tracking loop

Track a live MMA event by re-running the scrapers on a 5-minute loop until
the main event's result lands in the DB.

## Inputs

- **Org** — which promotion is live. Default `UFC` if the user doesn't say.
  Must match an `organization` value in the events table.
- Today's date = the event date. Confirm the event exists:
  `select id, name from events where organization = '<org>' and date = current_date`
  (prod project `tzmsetedkjsrlfgavdyq`). If no event row exists yet, run the
  Sherdog scraper once first — the event should appear.

## Setup

1. Create the loop with CronCreate (session-only, `*/5 * * * *`,
   `recurring: true`). The prompt must instruct: run
   `python sherdog_scraper.py --orgs "<org>"` then `python paramount_scraper.py`
   in the repo root, report a one-line summary of each, keep looping on errors.
   - **Scope Sherdog to the live org** — a full active-org sweep (~40 orgs)
     can overrun the 5-minute interval; the nightly covers the rest.
   - Paramount only matters for UFC/DWCS-era events (Paramount+ content);
     for non-UFC orgs skip it and just loop Sherdog.
2. Run the first pass immediately — don't wait for the first cron fire.
   Record the baseline numbers.

## Each pass

1. `python sherdog_scraper.py --orgs "<org>" 2>&1 | Select-Object -Last 4`
2. Check the top of tonight's card (results fill bottom-up; the main event
   posts last, often 30–60 min after the event ends):
   ```sql
   select fi.fight_position, f1.name || ' vs ' || f2.name as bout, fr.method
   from events e join fights fi on fi.event_id = e.id
   join fighters f1 on f1.id = fi.fighter1_id
   join fighters f2 on f2.id = fi.fighter2_id
   left join fight_results fr on fr.fight_id = fi.id
   where e.organization = '<org>' and e.date = current_date
     and fi.fight_position <= 3
   order by fi.fight_position;
   ```
3. `python paramount_scraper.py 2>&1 | Select-Object -Last 8` (UFC only)
4. Report ONE short line: Sherdog fights pushed + which top-card results are
   in; Paramount clips/event-links/unmatched with deltas vs the previous pass.

## Reading the numbers

- **Paramount unmatched climbing** during/after the card is normal — clips
  upload ahead of Sherdog results; they convert on later passes/nightly.
- **Paramount totals drifting a few ±** between passes is listing jitter;
  larger drops usually mean Paramount rotated clips out of its listings
  (nothing is lost — the scraper never clears stored URLs).
- **Sherdog "pushed N" holding steady** just means the same in-progress
  events re-pushed; watch the results query, not the push count.

## Stopping

- Auto-stop when `fight_position = 1` has a result (or when the user says
  the event is done): `CronDelete` the job, then post a wrap-up — final
  result lines for the top of the card, Paramount deltas for the night, and
  a reminder that remaining unmatched clips convert via the nightly.
- Videos for the event itself (Fight Pass replays, DWCS episodes, YouTube
  cards) arrive days later — the standing scrapers pick them up; no need to
  keep this loop alive for them.
