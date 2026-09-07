# Target Database Model

The intended end-state schema for the "every pro event" vision. This is a
**design target**, not the current DB — see "Changes from current" at the end.

## Relationships

```
organizations ──< events ──< fights ──1:1── fight_results
                               │  │
                   fighters ──<┘  └──< ratings   (your personal data)
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
app/lookup convenience. Carries the per-fight video URLs plus the
event-level ones (`event_paramount_url`, `event_fightpass_url`,
`event_fightpass_prelims_url`, `event_youtube_url`,
`event_youtube_prelims_url`, `event_pluto_url`) so the app can fall back to
the full-card replay when a fight has no dedicated VOD. `security_invoker=on`.
`events.youtube_prelims_url` was added 2026-08-15 for LFA's prelim-card
replay uploads. `pluto_url` on both `events` and `fights` (plus `fi.pluto_url`
/ `event_pluto_url` on the view) was added 2026-09-06 via
`migrate/add_pluto_url.sql` for Pluto TV's free Bellator replays, filled by
`pluto_scraper.py` — a one-off backfill (Bellator is defunct), not a nightly
step. No prelims variant: Pluto posts one replay per card.

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
