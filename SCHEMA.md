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
| nationality    | text | optional, from fighter page |
| date_of_birth  | date | optional |

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
| fighter1_rank / 2    | int  | UFC ranking, nullable |
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

### fight_search (view)
Denormalized join of fights + fighters + events + fight_results for
app/lookup convenience. Carries the per-fight video URLs plus the
event-level ones (`event_paramount_url`, `event_fightpass_url`,
`event_fightpass_prelims_url`) so the app can fall back to the full-card
replay when a fight has no dedicated VOD. `security_invoker=on`.

## Changes from current

| area | current | target |
|------|---------|--------|
| fighter / event ids | UUID hashed from name(+date) | **Sherdog numeric ids** |
| organization | free-text column on events | **`organizations` table** + FK |
| event date | text ("April 11, 2026") | `date` type |
| video links | per-platform columns (`paramount_url`, `espn_url`, `youtube_url`, `netflix_url`, `espn_prelims_url`) on events + fights | one **`video_links`** table |
| weight class | free-text column, `"Women's "` prefixed in | **`weight_classes`** lookup + FK + `catch_weight_lbs`; men's & women's are separate rows |
| amateur fights | not distinguished | `is_amateur` flag (or excluded) |

The id change is the gating migration (Phase 1). The `organizations` and
`video_links` normalizations can follow as separate, lower-risk steps.
