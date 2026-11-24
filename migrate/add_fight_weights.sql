-- Fight weights (2026-11-23).
--
-- fight_weights: the weight class of a fight Sherdog lists without one (most
-- cards before 2012), from Wikipedia's bout tables (weight_scraper.py).
-- Kept apart from fights because sherdog_scraper.py rewrites
-- fights.weight_class on every re-scrape, which would blank it.
--
-- weight_class is the name the bout was billed under; weight_limit_lb is the
-- upper limit in pounds, null when the class has none (Openweight, Super
-- Heavyweight, the early UFC's 200-and-over Heavyweight). The same name
-- meant different weights in different eras (a 1999 UFC "Middleweight" was
-- under 200 lb), so the app shows the limit beside the name where it differs
-- from today's and groups by the limit.
--
-- fight_search.weight_class now falls back to this table when the fight's
-- own is blank, and the view gains weight_limit_lb at the end.

create table if not exists public.fight_weights (
  fight_id        text        primary key references public.fights(id) on delete cascade,
  weight_class    text        not null,
  weight_limit_lb numeric,
  source          text        not null default 'wikipedia',  -- 'rule' = set by era alone (UFC before weight classes)
  source_url      text,
  updated_at      timestamptz not null default now()
);

alter table public.fight_weights enable row level security;
drop policy if exists "fight_weights are public" on public.fight_weights;
create policy "fight_weights are public" on public.fight_weights
  for select to anon, authenticated using (true);

create or replace view public.fight_search with (security_invoker = on) as
 SELECT fi.id,
    COALESCE(NULLIF(fi.weight_class, ''), fw.weight_class) AS weight_class,   -- Sherdog's, else fight_weights
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
    sm.context       AS context,
    fw.weight_limit_lb                         -- upper limit in lb where fight_weights knows it
   FROM public.fights fi
     JOIN public.fighters f1 ON f1.id = fi.fighter1_id
     JOIN public.fighters f2 ON f2.id = fi.fighter2_id
     JOIN public.events e ON e.id = fi.event_id
     LEFT JOIN public.fight_results fr ON fr.fight_id = fi.id
     LEFT JOIN public.fighters w ON w.id = fr.winner_id
     LEFT JOIN public.fight_crowd_ratings cr ON cr.fight_id = fi.id AND cr.source = 'verdict'
     LEFT JOIN public.fight_odds o ON o.fight_id = fi.id AND o.source = 'bfo'
     LEFT JOIN public.fight_summaries sm ON sm.fight_id = fi.id
     LEFT JOIN public.fight_weights fw ON fw.fight_id = fi.id;
