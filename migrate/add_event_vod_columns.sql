-- 2026-09-07: surface the remaining event-level replay links on fight_search
-- (event_espn_url, event_espn_prelims_url, event_netflix_url) so a fight row
-- carries every event VOD the events table knows about. The app falls back to
-- the event replay whenever a fight has no dedicated video, and hides the
-- result (spoiler) in that case too — on the fighter card as well as the
-- event card, so the row can't depend on which event happens to be open.
-- Columns are appended at the end — CREATE OR REPLACE VIEW requires the
-- existing column list to be a prefix of the new one.
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
    e.pluto_url AS event_pluto_url,
    e.espn_url AS event_espn_url,
    e.espn_prelims_url AS event_espn_prelims_url,
    e.netflix_url AS event_netflix_url
   FROM fights fi
     JOIN fighters f1 ON f1.id = fi.fighter1_id
     JOIN fighters f2 ON f2.id = fi.fighter2_id
     JOIN events e ON e.id = fi.event_id
     LEFT JOIN fight_results fr ON fr.fight_id = fi.id
     LEFT JOIN fighters w ON w.id = fr.winner_id;
