-- Crowd ratings + UFC bonus awards (2026-09-16).
--
-- fight_crowd_ratings / event_crowd_ratings: community fight + event scores
-- scraped from Verdict MMA (verdictmma.com) by verdict_scraper.py. Kept apart
-- from `ratings` (the user's own, irreplaceable) — these are "other users" and
-- the app blends them in with a weight that grows with rating_count.
--
-- fight_bonuses: UFC post-fight bonus awards (Fight / Performance / Knockout /
-- Submission of the Night) from the Wikipedia event pages, by
-- bonus_scraper.py. Objective, covers the whole UFC back-catalog, and feeds a
-- small fixed bump in the app's blended score.
--
-- fight_search gains crowd_rating, crowd_rating_count, crowd_source and
-- bonus_awards so a fight row stays self-contained.

create table if not exists public.fight_crowd_ratings (
  fight_id      text        not null references public.fights(id) on delete cascade,
  source        text        not null default 'verdict',   -- 'verdict' (room for more)
  rating        numeric(4,2) not null,                    -- 0–10 as the source shows it
  rating_count  integer     not null,
  source_key    text,                                     -- source's own id, e.g. verdict "1919/14" (event/fight)
  source_url    text,
  scraped_at    timestamptz not null default now(),
  primary key (fight_id, source)
);

create table if not exists public.event_crowd_ratings (
  event_id      bigint      not null references public.events(id) on delete cascade,
  source        text        not null default 'verdict',
  rating        numeric(4,2),                             -- event rating 0–10
  rating_count  integer,
  hype          numeric(4,2),                             -- pre-event hype 0–10
  hype_count    integer,
  source_key    text,                                     -- verdict event id
  source_url    text,
  scraped_at    timestamptz not null default now(),
  primary key (event_id, source)
);

create table if not exists public.fight_bonuses (
  id            bigserial   primary key,
  fight_id      text        not null references public.fights(id) on delete cascade,
  award         text        not null,                     -- 'FOTN' | 'POTN' | 'KOTN' | 'SOTN'
  fighter_id    bigint      references public.fighters(id) on delete set null,  -- null for FOTN (both fighters)
  source_url    text,
  scraped_at    timestamptz not null default now()
);
-- one row per (fight, award, fighter); FOTN has no fighter so coalesce to 0
create unique index if not exists fight_bonuses_key
  on public.fight_bonuses (fight_id, award, coalesce(fighter_id, 0));
create index if not exists fight_bonuses_fight_idx on public.fight_bonuses (fight_id);
create index if not exists event_crowd_ratings_source_key_idx on public.event_crowd_ratings (source, source_key);

-- Read-only to the app; the scrapers write with the service key.
alter table public.fight_crowd_ratings enable row level security;
alter table public.event_crowd_ratings enable row level security;
alter table public.fight_bonuses       enable row level security;
drop policy if exists "fight_crowd_ratings are public" on public.fight_crowd_ratings;
create policy "fight_crowd_ratings are public" on public.fight_crowd_ratings
  for select to anon, authenticated using (true);
drop policy if exists "event_crowd_ratings are public" on public.event_crowd_ratings;
create policy "event_crowd_ratings are public" on public.event_crowd_ratings
  for select to anon, authenticated using (true);
drop policy if exists "fight_bonuses are public" on public.fight_bonuses;
create policy "fight_bonuses are public" on public.fight_bonuses
  for select to anon, authenticated using (true);

-- fight_search: same columns as before (order preserved — `create or replace`
-- only allows appending) plus the crowd + bonus columns at the end.
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
    cr.rating       AS crowd_rating,        -- 0–10, null when no crowd data
    cr.rating_count AS crowd_rating_count,
    cr.source       AS crowd_source,
    ( SELECT string_agg(b.award, ',' ORDER BY b.award)
        FROM public.fight_bonuses b WHERE b.fight_id = fi.id ) AS bonus_awards  -- e.g. 'FOTN' / 'POTN' / 'FOTN,POTN'
   FROM public.fights fi
     JOIN public.fighters f1 ON f1.id = fi.fighter1_id
     JOIN public.fighters f2 ON f2.id = fi.fighter2_id
     JOIN public.events e ON e.id = fi.event_id
     LEFT JOIN public.fight_results fr ON fr.fight_id = fi.id
     LEFT JOIN public.fighters w ON w.id = fr.winner_id
     LEFT JOIN public.fight_crowd_ratings cr ON cr.fight_id = fi.id AND cr.source = 'verdict';
