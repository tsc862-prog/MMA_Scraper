-- Betting odds (2026-10-06).
--
-- fight_odds: opening and closing moneylines per fight from BestFightOdds
-- (bestfightodds.com), scraped by bfo_scraper.py. BFO's fighter pages carry
-- every bout since 2007 with an "Open" line and a "Closing range" (the lowest
-- and highest closing line across the books it tracks); both fighters' sides
-- are stored, in the DB's fighter1 / fighter2 order, as American odds
-- (-210 = favourite, +175 = underdog). `fighterN_close` is the midpoint of
-- the closing range (averaged in decimal odds, converted back) — the single
-- number the app shows. For an upcoming fight the "closing" columns hold the
-- current line until the nightly refresh after the card.
--
-- Separate from fights on purpose: it's a third-party signal with its own
-- source key, and an upset (the winner closed as the underdog) is a strong
-- rewatch recommendation the app can derive from these four numbers plus
-- fight_results.winner_id.
--
-- fight_search gains fighter1_odds_open / fighter1_odds_close /
-- fighter2_odds_open / fighter2_odds_close so a fight row stays self-contained.

create table if not exists public.fight_odds (
  fight_id            text        not null references public.fights(id) on delete cascade,
  source              text        not null default 'bfo',   -- 'bfo' (room for more)
  fighter1_open       integer,                              -- American odds
  fighter1_close_low  integer,
  fighter1_close_high integer,
  fighter1_close      integer,                              -- midpoint of the closing range
  fighter2_open       integer,
  fighter2_close_low  integer,
  fighter2_close_high integer,
  fighter2_close      integer,
  source_key          text,                                 -- BFO matchup id
  source_url          text,                                 -- the BFO event page
  scraped_at          timestamptz not null default now(),
  primary key (fight_id, source)
);
create index if not exists fight_odds_source_key_idx on public.fight_odds (source, source_key);

-- Read-only to the app; the scraper writes with the service key.
alter table public.fight_odds enable row level security;
drop policy if exists "fight_odds are public" on public.fight_odds;
create policy "fight_odds are public" on public.fight_odds
  for select to anon, authenticated using (true);

-- fight_search: same columns as before (order preserved — `create or replace`
-- only allows appending) plus the four odds columns at the end.
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
    o.fighter2_close AS fighter2_odds_close
   FROM public.fights fi
     JOIN public.fighters f1 ON f1.id = fi.fighter1_id
     JOIN public.fighters f2 ON f2.id = fi.fighter2_id
     JOIN public.events e ON e.id = fi.event_id
     LEFT JOIN public.fight_results fr ON fr.fight_id = fi.id
     LEFT JOIN public.fighters w ON w.id = fr.winner_id
     LEFT JOIN public.fight_crowd_ratings cr ON cr.fight_id = fi.id AND cr.source = 'verdict'
     LEFT JOIN public.fight_odds o ON o.fight_id = fi.id AND o.source = 'bfo';
