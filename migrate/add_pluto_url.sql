-- add_pluto_url.sql — Pluto TV replay links (Bellator).
--
-- Pluto TV (free, ad-supported, Paramount-owned) carries two Bellator series:
--   "Bellator MMA Full Fight Cards"   — 136 full-event replays, Bellator 58–301
--   "Bellator MMA Individual Fights"  — ~200 single-bout VODs, Bellator 140–239
-- pluto_scraper.py matches them into these columns. One column per table, like
-- youtube_url / fightpass_url — the polymorphic video_links table in SCHEMA.md
-- is still a design target, not the live schema.
--
-- No prelims variant: Pluto posts one replay per card.

alter table events add column if not exists pluto_url text;
alter table fights add column if not exists pluto_url text;

-- Surface both on fight_search so the app can fall back to the full-card
-- replay when a fight has no dedicated VOD (same pattern as the other
-- platforms). Columns are appended at the end — CREATE OR REPLACE VIEW
-- requires the existing column list to be a prefix of the new one.
create or replace view fight_search with (security_invoker=on) as
 SELECT fi.id,
    fi.weight_class,
    fi.is_main,
    fi.is_title,
    fi.fight_position,
    fi.fight_position_type,
    f1.id AS fighter1_id,
    f1.name AS fighter1_name,
    f2.id AS fighter2_id,
    f2.name AS fighter2_name,
    e.id AS event_id,
    e.name AS event_name,
    e.date AS event_date,
    e.location AS event_location,
    fr.winner_id,
    w.name AS winner_name,
    fr.method,
    fr.details,
    fr.round,
    fr."time",
    fi.fighter1_rank,
    fi.fighter2_rank,
    fi.notes,
    fi.paramount_url,
    e.paramount_url AS event_paramount_url,
    fi.fighter1_is_debut,
    fi.fighter2_is_debut,
    e.organization AS event_organization,
    fi.youtube_url,
    fi.fightpass_url,
    fi.is_amateur,
    e.fightpass_url AS event_fightpass_url,
    e.fightpass_prelims_url AS event_fightpass_prelims_url,
    e.youtube_url AS event_youtube_url,
    e.youtube_prelims_url AS event_youtube_prelims_url,
    fi.pluto_url,
    e.pluto_url AS event_pluto_url
   FROM fights fi
     JOIN fighters f1 ON f1.id = fi.fighter1_id
     JOIN fighters f2 ON f2.id = fi.fighter2_id
     JOIN events e ON e.id = fi.event_id
     LEFT JOIN fight_results fr ON fr.fight_id = fi.id
     LEFT JOIN fighters w ON w.id = fr.winner_id;
