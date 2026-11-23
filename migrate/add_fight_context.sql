-- Fight context (2026-11-23).
--
-- Two pre-fight facts on fight_summaries, pulled from the same Sherdog
-- play-by-play article in the same model request as the summary
-- (summary_scraper.py): which fighter missed weight, and which took the bout
-- as a late replacement, plus one short sentence of detail. Unlike the
-- summary none of it gives the result away, so the app shows it ungated.
-- False means "the article did not say", not "confirmed on weight".
--
-- fight_search gains the five columns at the end.

alter table public.fight_summaries
  add column if not exists fighter1_missed_weight boolean not null default false,
  add column if not exists fighter2_missed_weight boolean not null default false,
  add column if not exists fighter1_short_notice  boolean not null default false,
  add column if not exists fighter2_short_notice  boolean not null default false,
  add column if not exists context                text;

create or replace view public.fight_search with (security_invoker = on) as
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
    e.netflix_url AS event_netflix_url,
    cr.rating       AS crowd_rating,
    cr.rating_count AS crowd_rating_count,
    cr.source       AS crowd_source,
    ( SELECT string_agg(b.award, ',' ORDER BY b.award)
        FROM public.fight_bonuses b WHERE b.fight_id = fi.id ) AS bonus_awards,
    o.fighter1_open  AS fighter1_odds_open,    -- American odds, null when BFO has no line
    o.fighter1_close AS fighter1_odds_close,
    o.fighter2_open  AS fighter2_odds_open,
    o.fighter2_close AS fighter2_odds_close,
    sm.summary       AS summary,               -- null until summary_scraper.py has written one
    sm.fighter1_missed_weight,                 -- pre-fight context, null when the fight has no summary row
    sm.fighter2_missed_weight,
    sm.fighter1_short_notice,
    sm.fighter2_short_notice,
    sm.context       AS context
   FROM public.fights fi
     JOIN public.fighters f1 ON f1.id = fi.fighter1_id
     JOIN public.fighters f2 ON f2.id = fi.fighter2_id
     JOIN public.events e ON e.id = fi.event_id
     LEFT JOIN public.fight_results fr ON fr.fight_id = fi.id
     LEFT JOIN public.fighters w ON w.id = fr.winner_id
     LEFT JOIN public.fight_crowd_ratings cr ON cr.fight_id = fi.id AND cr.source = 'verdict'
     LEFT JOIN public.fight_odds o ON o.fight_id = fi.id AND o.source = 'bfo'
     LEFT JOIN public.fight_summaries sm ON sm.fight_id = fi.id;
