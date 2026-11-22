# Target Database Model

The intended end-state schema for the "every pro event" vision. This is a
**design target**, not the current DB — see "Changes from current" at the end.

## Relationships

```
organizations ──< events ──< fights ──1:1── fight_results
                               │  │
                   fighters ──<┘  └──< ratings   (your personal data)
                                    ├──< fight_crowd_ratings  (Verdict MMA fans)
                                    ├──< fight_bonuses        (UFC bonus awards)
                                    └──< fight_odds           (BestFightOdds lines)
                      │
                      └──< fighter_aliases

video_links  >── fights | events     (one row per video, any platform)
change_log         audit of fights / events / fight_results
review_queue       scraper items needing a human decision
```

`──<` = one-to-many. `>──` = many-to-one (polymorphic).

## Tables

### organizations
Promotions. New table — replaces the free-text `events.organization`.
| column        | type | notes |
|---------------|------|-------|
| id            | bigint PK | Sherdog organization id |
| name          | text | "Ultimate Fighting Championship", "Bellator MMA", … |
| status        | text | active / defunct |

### fighters
| column         | type | notes |
|----------------|------|-------|
| id             | bigint PK | **Sherdog fighter id** (stable; replaces name-hash UUID) |
| name           | text |
| gender         | text | 'M' / 'F' — from propagation + Wikidata |
| gender_checked | bool | Wikidata lookup done |
| image_url      | text | Sherdog portrait (200×300); null when Sherdog has no photo |
| nationality    | text | optional, from fighter page |
| date_of_birth  | date | optional |
| height         | text | as displayed, e.g. `6'4"` |
| association    | text | camp / team |
| profile_scraped_at | timestamptz | null = fighter page never crawled |

The five profile columns plus `profile_scraped_at` are filled by
`fighter_profile_scraper.py` (added 2026-09-05, `migrate/add_fighter_profile.sql`),
which crawls `/fighter/<id>` — `sherdog_scraper.py` only reads event pages, which
carry nothing but a fighter's name and id. `profile_scraped_at` doubles as the
crawler's resume marker, so there's no tracker file.

There is deliberately **no** `sherdog_url` column: `fighters.id` *is* the Sherdog
fighter id and `https://www.sherdog.com/fighter/<id>` redirects to the canonical
slug URL, so the app builds the link from the id it already has.

Sherdog **403s hotlinked images when a `Referer` is sent**, so any `<img>` using
`image_url` must carry `referrerpolicy="no-referrer"`.

### fighter_aliases
| column     | type | notes |
|------------|------|-------|
| alias      | text PK | lowercased name variant |
| fighter_id | bigint FK → fighters |

### weight_classes
Canonical divisions — a fixed lookup. Men's and women's divisions are
**separate rows** (e.g. "Flyweight" and "Women's Flyweight"). Catchweights /
oddities use a sentinel row plus `fights.catch_weight_lbs` for the poundage.
| column          | type | notes |
|-----------------|------|-------|
| id              | int PK |
| name            | text | "Flyweight", "Women's Flyweight", … + Catchweight / Open Weight / Unknown |
| gender          | text | 'M' / 'F' |
| upper_limit_lbs | int  | division weight limit, null for sentinels |
| sort_order      | int  | lightest → heaviest for display |

Gender determination (propagation + Wikidata + review queue) still runs — it
decides whether a Sherdog fight (Sherdog doesn't gender-label) maps to the
men's or women's division row.

### events
| column          | type | notes |
|-----------------|------|-------|
| id              | bigint PK | **Sherdog event id** (stable) |
| name            | text |
| date            | date | proper date type (currently text) |
| location        | text |
| organization_id | bigint FK → organizations |

### fights
| column               | type | notes |
|----------------------|------|-------|
| id                   | text PK | deterministic: hash of event_id + sorted fighter ids |
| event_id             | bigint FK → events |
| fighter1_id          | bigint FK → fighters |
| fighter2_id          | bigint FK → fighters |
| weight_class_id      | int FK → weight_classes |
| catch_weight_lbs     | int  | actual poundage when division = Catchweight |
| is_main / is_title   | bool |
| fight_position       | int  | 1 = main event |
| fight_position_type  | text |
| fighter1_rank / 2    | text | the UFC rank each fighter carried **into** the bout — `'C'` champion, `'1'`..`'15'`, null unranked. Since 2026-09-06 `rankings_scraper.py` stamps these on **upcoming UFC fights** from the latest UFC rankings snapshot and never touches a fight dated before today, so they freeze at fight-night values. Not backfilled for past fights (historical ranks are a separate job). |
| fighter1_is_debut / 2| bool | first fight in that org — computed; same-date ties (early UFC tournaments) resolve to the fight lowest on the card (highest fight_position) |
| is_amateur           | bool | amateur bouts flagged (or excluded entirely) |
| notes                | text |

### fight_results
One row per completed fight.
| column     | type | notes |
|------------|------|-------|
| fight_id   | text PK, FK → fights |
| winner_id  | bigint FK → fighters | null = draw / no-contest |
| method     | text | "KO/TKO", "Submission", "Decision", … |
| details    | text | "Rear Naked Choke", "Punches", judges' scores |
| round      | int |
| time       | text |

### video_links
New — normalizes the scattered `*_url` columns on events/fights into one table.
| column      | type | notes |
|-------------|------|-------|
| id          | bigint PK |
| entity_type | text | 'fight' or 'event' |
| entity_id   | text | fight id or event id |
| platform    | text | youtube / paramount / espn / netflix / … |
| variant     | text | full_fight / full_event / main_card / prelims / highlight |
| url         | text |
| unique (entity_type, entity_id, platform, variant) |

### ratings
Your personal fight ratings — irreplaceable; survives every migration.
| column    | type | notes |
|-----------|------|-------|
| id        | uuid PK |
| fight_id  | text FK → fights |
| user_id   | uuid |
| rating    | numeric(3,1) |
| notes     | text |
| logged_at | bigint |

### fight_crowd_ratings
Community ("other users") fight scores, added 2026-09-16 via
`migrate/add_crowd_ratings.sql`, filled nightly by `verdict_scraper.py`.
Separate from `ratings` on purpose — nothing here is yours. RLS on, anon
`select` only.
| column       | type | notes |
|--------------|------|-------|
| fight_id     | text FK → fights (cascade) |
| source       | text | `verdict` (verdictmma.com); room for more sources |
| rating       | numeric(4,2) | on the source's scale — Verdict is **0–10**; the app halves it to stars |
| rating_count | int | how many fans rated; drives the blend weight (full at 50) |
| source_key   | text | Verdict's own key, `"<event id>/<fight index>"` |
| source_url   | text |
| scraped_at   | timestamptz | last refresh — pages are re-fetched for 30 days after the card |
| primary key (fight_id, source) |

Since 2026-09-16 (`migrate/community_crowd_ratings.sql`) the app's
`community_dashboard()` RPC reads its per-fight average / count from this
table (rating ÷ 2, `rating_count`) instead of `ratings` — the crowd stands in
for "other users" until the app has real ones.

### event_crowd_ratings
Same source, per event: `event_id` FK → events (cascade), `source`, `rating`
/ `rating_count` (post-event score 0–10), `hype` / `hype_count` (pre-event
hype 0–10), `source_key` (Verdict event id), `source_url`, `scraped_at`.
Primary key (event_id, source). Not yet surfaced in the app.

### fight_bonuses
UFC post-fight bonus awards from the Wikipedia event pages, added 2026-09-16
(`migrate/add_crowd_ratings.sql`), filled by `bonus_scraper.py` (nightly for
recent cards; `--all` backfilled the catalog). RLS on, anon `select` only.
| column     | type | notes |
|------------|------|-------|
| id         | bigserial PK |
| fight_id   | text FK → fights (cascade) |
| award      | text | `FOTN` (Fight of the Night), `POTN` (Performance), `KOTN` / `SOTN` (Knockout / Submission, pre-2014) |
| fighter_id | bigint FK → fighters, nullable | the awarded fighter; **null for FOTN** (both) |
| source_url | text | the Wikipedia article |
| scraped_at | timestamptz |
| unique (fight_id, award, coalesce(fighter_id, 0)) |

### fight_odds
Betting odds per fight from BestFightOdds (bestfightodds.com), added
2026-10-06 (`migrate/add_fight_odds.sql`), filled by `bfo_scraper.py`
(nightly for cards within ±10 days; `--sweep` backfills the catalog from
BFO's fighter pages, which go back to 2007). American odds (`-210` favourite,
`+175` underdog), both sides stored in the DB's fighter1 / fighter2 order.
RLS on, anon `select` only.
| column              | type | notes |
|---------------------|------|-------|
| fight_id            | text FK → fights (cascade) |
| source              | text | `bfo` (room for more) |
| fighter1_open       | int  | opening line |
| fighter1_close_low  | int  | lowest closing line across the books BFO tracks |
| fighter1_close_high | int  | highest closing line |
| fighter1_close      | int  | midpoint of the closing range (averaged in decimal odds, converted back) — the number the app shows |
| fighter2_open / _close_low / _close_high / _close | int | same for fighter 2 |
| source_key          | text | BFO matchup id (or `<bfo event id>/<slug>/<slug>` for a bout BFO hasn't charted yet) |
| source_url          | text | the BFO event page |
| scraped_at          | timestamptz |
| primary key (fight_id, source) |

For an **upcoming** fight the `close` columns hold the current line; the
nightly re-fetches every card within the window, so they settle to the true
close after the bell. An upset = `winner_id`'s side closed positive.

### fight_summaries
A short written account (2–4 sentences) of how each bout played out, added
2026-11-21 (`migrate/add_fight_summaries.sql`),
filled by `summary_scraper.py`: a Claude model writes it from Sherdog's
round-by-round play-by-play article for the card. Only the generated summary
is stored, never the play-by-play text. One row per fight; `--force`
overwrites. RLS on, anon `select` only.
| column       | type | notes |
|--------------|------|-------|
| fight_id     | text PK, FK → fights (cascade) |
| summary      | text | 2–4 sentences, gives the result away — the app shows it only after the viewer rates the fight |
| source       | text | `sherdog` (where the play-by-play came from) |
| source_url   | text | the play-by-play article |
| model        | text | model id that wrote it, e.g. `claude-sonnet-5-5` |
| generated_at | timestamptz |

### change_log
Audit of inserts/updates/deletes on fights, events, fight_results.
(Triggers disabled during the bulk backfill.)

### review_queue
Scraper items needing a human decision (gender, alias suggestions).

### rankings
History of published rankings for the app's Rankings page — added 2026-09-06
via `migrate/add_rankings.sql`, filled nightly by `rankings_scraper.py`. The
table is a set of **dated snapshots**: every run scrapes the current lists and,
when they differ from the latest stored snapshot for that source, stores them
under today's `snapshot_date`. Earlier snapshots are never modified, so the
app can show the rankings "as of" any stored date. RLS on, anon `select` only.
| column        | type | notes |
|---------------|------|-------|
| id            | bigserial PK |
| source        | text | `ufc` (ufc.com/rankings) or `sherdog` (Sherdog's rankings articles) |
| snapshot_date | date | the day (Pacific) this set was captured; a same-day re-run replaces that day's set |
| division      | text | canonical: "Heavyweight", "Women's Flyweight", "Men's Pound-for-Pound", "Women's Pound-for-Pound", … |
| rank          | int  | **0 = champion** (UFC divisions only; Sherdog and P4P lists have no champion slot) |
| fighter_id    | bigint FK → fighters, nullable | null when the name couldn't be matched (UFC) or the fighter isn't in the DB (Sherdog) |
| fighter_name  | text | as the source displays it — shown when `fighter_id` is null |
| previous_rank | int, nullable | from the source's movement marker; null for champions / new entries |
| is_new        | bool | NR / new entry this period |
| record        | text, nullable | Sherdog only, e.g. "14-2, 1 NC" |
| promotion     | text, nullable | Sherdog only — the fighter's current org ("UFC", "PFL", "Rizin") |
| source_url    | text | the fighter's page on the source site (ufc.com/athlete/… or sherdog.com/fighter/…) |
| scraped_at    | timestamptz | when this snapshot was taken |
| unique (source, snapshot_date, division, rank, fighter_name) — the name is in the key so a published tie (ufc.com has shown two #10s) keeps both rows |

Two helper views (`security_invoker=on`): **`rankings_current`** — the rows of
the newest snapshot per source (what the app shows by default); and
**`rankings_snapshots`** — one row per (source, snapshot_date) with an entry
count, feeding the app's "as of" date picker.

### fight_search (view)
Denormalized join of fights + fighters + events + fight_results for
app/lookup convenience. Carries the per-fight video URLs plus **every**
event-level one (`event_paramount_url`, `event_espn_url`,
`event_espn_prelims_url`, `event_fightpass_url`,
`event_fightpass_prelims_url`, `event_youtube_url`,
`event_youtube_prelims_url`, `event_netflix_url`, `event_pluto_url`) so a
fight row is self-contained: when a fight has no dedicated VOD the app shows
the event replay in its place (full-card link preferred over prelims) and
hides the result as a spoiler, wherever the row renders. The ESPN / Netflix
event columns were appended 2026-09-07 via
`migrate/add_event_vod_columns.sql`. `security_invoker=on`.
Since 2026-09-16 (`migrate/add_crowd_ratings.sql`) the row also carries
`crowd_rating` / `crowd_rating_count` / `crowd_source` (the Verdict row from
`fight_crowd_ratings`, null when none) and `bonus_awards` (the fight's
`fight_bonuses` codes joined with commas, e.g. `FOTN,POTN`, null when none).
The app blends these with the user's stars as a quiet secondary number.
Since 2026-10-06 (`migrate/add_fight_odds.sql`) it also carries
`fighter1_odds_open` / `fighter1_odds_close` / `fighter2_odds_open` /
`fighter2_odds_close` (American odds from `fight_odds`, null when BFO has no
line).
Since 2026-11-21 (`migrate/add_fight_summaries.sql`) it also carries
`summary` (the `fight_summaries` text, null when none).
`events.youtube_prelims_url` was added 2026-08-15 for LFA's prelim-card
replay uploads. `pluto_url` on both `events` and `fights` (plus `fi.pluto_url`
/ `event_pluto_url` on the view) was added 2026-09-06 via
`migrate/add_pluto_url.sql` for Pluto TV's free Bellator replays, filled by
`pluto_scraper.py` — a one-off backfill (Bellator is defunct), not a nightly
step. No prelims variant: Pluto posts one replay per card.

### App read models (materialized views + RPCs)
Added 2026-09-16 via `migrate/app_perf_precompute.sql` so the app's slow
screens do one round trip against precomputed data instead of re-joining
66k fights on every page load (on the small prod instance that was 0.5–2 s
for the event flags, 1.6 s for the community dashboard, and the events table
was paged down in 7 requests). Both views are refreshed by
**`refresh_app_caches()`** (service_role only, `CONCURRENTLY` so readers
never block), which `refresh_app_caches.py` calls at the end of every scraper
pass — nightly workflow and live-event loop. Neither view is exposed through
the API; the app only reaches them through the RPCs.

| object | kind | what it holds |
|--------|------|---------------|
| `event_flags_mv` | matview, unique on `event_id` | per event: `has_results` (any fight with a method) and `has_video` (any fight with a paramount / youtube / fightpass / pluto URL); amateur fights excluded |
| `community_fight_base` | matview, unique on `id`; indexes on `organization`, `event_year` | one row per pro fight: event name / org / year, weight class, both fighter ids + names, winner, method, precomputed `is_dec` / `is_finish` / `is_ko` / `is_sub` and `time_sec` |
| `events_index()` | RPC → jsonb, security definer | every `events` row (nulls stripped) + `has_results` / `has_fight_video` from `event_flags_mv`, as one array — replaces `event_result_flags()` + paging `events` |
| `my_ratings()` | RPC → jsonb, **security invoker** | the caller's `ratings` rows (RLS: `user_id = auth.uid()`) each overlaid on its `fight_search` row, newest first — ratings keys win (`id`, `notes`), mirroring the app's old client-side merge |
| `community_dashboard()` | RPC → jsonb, security definer, **plpgsql + EXECUTE** | unchanged signature and output; now aggregates `community_fight_base` (+ live `ratings`) instead of re-joining the base tables. The org / division / year filters are inlined as literals on purpose: as a SQL-language function PG17 planned the body with the parameters unknown, estimated the filtered base at ~1 row, and nested-looped the ratings aggregate once per fight (35 s+) |
| `refresh_app_caches()` | RPC → void, security definer, **service_role only** | refreshes both views |

`event_result_flags()` is kept for browsers still holding the previous app
JS; drop it once that has aged out.

## Changes from current

| area | current | target |
|------|---------|--------|
| fighter / event ids | UUID hashed from name(+date) | **Sherdog numeric ids** |
| organization | free-text column on events | **`organizations` table** + FK |
| event date | text ("April 11, 2026") | `date` type |
| video links | per-platform columns (`paramount_url`, `espn_url`, `youtube_url`, `netflix_url`, `fightpass_url`, `pluto_url`, `espn_prelims_url`, …) on events + fights | one **`video_links`** table |
| weight class | free-text column, `"Women's "` prefixed in | **`weight_classes`** lookup + FK + `catch_weight_lbs`; men's & women's are separate rows |
| amateur fights | not distinguished | `is_amateur` flag (or excluded) |

The id change is the gating migration (Phase 1). The `organizations` and
`video_links` normalizations can follow as separate, lower-risk steps.
