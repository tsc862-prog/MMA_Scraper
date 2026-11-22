-- Fight summaries (2026-11-21).
--
-- fight_summaries: a short written account (2–4 sentences) of how each bout
-- played out, written by a Claude model from Sherdog's round-by-round
-- play-by-play article for the card (summary_scraper.py). Only the generated
-- summary is stored — never the play-by-play text itself.
--
-- Separate from fights on purpose, like fight_odds and fight_crowd_ratings:
-- it's derived third-party content with its own source and model, and it can
-- be regenerated wholesale without touching the fight row.
--
-- One summary per fight (primary key fight_id); a re-run with --force
-- overwrites it. `model` records which model actually wrote the text.
--
-- fight_search gains `summary` at the end so a fight row stays
-- self-contained. The app shows it behind a "Summary" toggle only after the
-- viewer has rated the fight (it gives the result away).

create table if not exists public.fight_summaries (
  fight_id     text        primary key references public.fights(id) on delete cascade,
  summary      text        not null,
  source       text        not null default 'sherdog',   -- where the play-by-play came from
  source_url   text,                                      -- the play-by-play article
  model        text,                                      -- model id that wrote the summary
  generated_at timestamptz not null default now()
);

-- Read-only to the app; the scraper writes with the service key.
alter table public.fight_summaries enable row level security;
drop policy if exists "fight_summaries are public" on public.fight_summaries;
create policy "fight_summaries are public" on public.fight_summaries
  for select to anon, authenticated using (true);

-- fight_search: same columns as before (order preserved — `create or replace`
-- only allows appending) plus `summary` at the end.
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
    sm.summary       AS summary                -- null until summary_scraper.py has written one
   FROM public.fights fi
     JOIN public.fighters f1 ON f1.id = fi.fighter1_id
     JOIN public.fighters f2 ON f2.id = fi.fighter2_id
     JOIN public.events e ON e.id = fi.event_id
     LEFT JOIN public.fight_results fr ON fr.fight_id = fi.id
     LEFT JOIN public.fighters w ON w.id = fr.winner_id
     LEFT JOIN public.fight_crowd_ratings cr ON cr.fight_id = fi.id AND cr.source = 'verdict'
     LEFT JOIN public.fight_odds o ON o.fight_id = fi.id AND o.source = 'bfo'
     LEFT JOIN public.fight_summaries sm ON sm.fight_id = fi.id;
