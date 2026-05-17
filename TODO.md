# MMA Scraper — Backlog

Future work, roughly prioritized. Each item carries notes on the intended
approach so it can be picked up cold in a later session.

## Organizations

### DONE — Bellator, WSOF  (added 2026-05-17)
- Bellator (315 events) + WSOF (37 events) scraped from Sherdog, both marked
  `active: false` in `SHERDOG_ORGS` (defunct — one-time scrape, skipped daily).
- Re-run with `python sherdog_scraper.py --all` if ever needed.

### Other orgs worth evaluating  — research scrapability per org
All are listed as organizations on Sherdog, so `sherdog_scraper.py` extends to
each via a config entry; video sourcing needs per-org research.
- **ONE Championship** — major global promotion, large catalog.
- **KSW** — top European promotion (Poland).
- **Cage Warriors** — UK feeder league, notable alumni.
- **Invicta FC** — women's MMA; would enrich women's-division data.
- **RIZIN** — Japan.

## Video sources for new orgs
- Generalize the YouTube scraper beyond PFL (it's currently PFL-channel-only).
  The PFL channel carries Bellator + WSOF; ONE/KSW/etc. have their own channels.
- Per org, identify the best video source (YouTube full-fight playlists,
  broadcaster catalogs) and note free vs. paywalled.

## App features
- **Fighter profile page** — per-fighter view: record, full fight history,
  debut flags, video links, gender, organizations competed in. Frontend work on
  the app, backed by the existing DB. The `fight_search` view already joins most
  of the needed data; a fighter-centric view or RPC may help.

## Ground rules (carried over from prior decisions)
- ufcstats stays the UFC source — do **not** re-key UFC off another source.
- Never change a Sherdog `fight_prefix` once data exists for that org.
- New non-UFC women's fighters resolve via gender propagation + Wikidata +
  the email review queue — no per-org gender handling needed.
