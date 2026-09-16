-- 2026-09-16: precompute what the app's slow screens were computing on every
-- page load. Applied to prod as migration app_perf_precompute.
--
-- Before: the event page ran a fights count, then event_result_flags() (a
-- 66k-row fights x fight_results aggregate, 0.5-2 s), then paged the whole
-- events table down in 7 sequential requests (~2 s). The community dashboard
-- RPC re-joined fights/fighters/events/results on a small instance (1.6 s).
-- Ratings loaded in two hops (ratings, then 13 chunked fight_search lookups).
--
-- After: two materialized views hold the expensive joins, refreshed by
-- refresh_app_caches() at the end of every scraper pass (nightly workflow and
-- the live-event loop). Three RPCs give the app one round trip per screen.
--
--   event_flags_mv        per-event has_results / has_video flags
--   community_fight_base  per-fight base row for the community dashboard
--   events_index()        every event + flags as one jsonb array
--   my_ratings()          the caller's ratings joined to fight_search
--   community_dashboard() rewritten over community_fight_base
--   refresh_app_caches()  service_role only; refreshes both views
--
-- event_result_flags() is left in place for browsers still holding the old
-- app JS; drop it once that has aged out.

-- ── event flags ──────────────────────────────────────────────────────────────
create materialized view if not exists public.event_flags_mv as
  select fi.event_id,
         bool_or(fr.method is not null) as has_results,
         bool_or(fi.paramount_url is not null or fi.youtube_url is not null
                 or fi.fightpass_url is not null or fi.pluto_url is not null) as has_video
  from fights fi
  left join fight_results fr on fr.fight_id = fi.id
  where fi.event_id is not null
    and not coalesce(fi.is_amateur, false)
  group by fi.event_id;

create unique index if not exists event_flags_mv_event_id_idx
  on public.event_flags_mv (event_id);

-- Every event row (nulls stripped to keep the payload small) plus the flags.
-- jsonb return: not subject to the API's max-rows cap.
create or replace function public.events_index()
returns jsonb
language sql stable security definer
set search_path to 'public'
as $$
  select coalesce(jsonb_agg(
           jsonb_strip_nulls(to_jsonb(e))
           || jsonb_build_object('has_results',     coalesce(f.has_results, false),
                                 'has_fight_video', coalesce(f.has_video,   false))
         ), '[]'::jsonb)
  from events e
  left join event_flags_mv f on f.event_id = e.id;
$$;

-- ── community dashboard base ─────────────────────────────────────────────────
-- The old RPC's `f` CTE minus the ratings join (ratings stay live: 1.3k rows,
-- and they change whenever a user rates). Method classes and fight time are
-- precomputed so the RPC never runs a regex over 100k rows.
create materialized view if not exists public.community_fight_base as
  select fi.id,
         fi.event_id,
         e.name as event_name,
         e.organization,
         extract(year from e.date)::int as event_year,
         fi.weight_class,
         fi.fighter1_id, f1.name as f1name,
         fi.fighter2_id, f2.name as f2name,
         fr.winner_id,
         fr.method,
         coalesce(fr.method ~* 'dec',    false) as is_dec,
         coalesce(fr.method ~* 'ko|sub', false) as is_finish,
         coalesce(fr.method ~* 'ko',     false) as is_ko,
         coalesce(fr.method ~* 'sub',    false) as is_sub,
         case
           when fr."time" ~ '^[0-9]+:[0-9]+$'
             then greatest(coalesce(fr.round,1)-1,0)*300
                  + split_part(fr."time",':',1)::int*60 + split_part(fr."time",':',2)::int
           when fr.round is not null then fr.round*300
           else 0
         end as time_sec
  from fights fi
  join fighters f1 on f1.id = fi.fighter1_id
  join fighters f2 on f2.id = fi.fighter2_id
  join events e on e.id = fi.event_id
  left join fight_results fr on fr.fight_id = fi.id
  where not coalesce(fi.is_amateur, false);

create unique index if not exists community_fight_base_id_idx
  on public.community_fight_base (id);
create index if not exists community_fight_base_org_idx
  on public.community_fight_base (organization);
create index if not exists community_fight_base_year_idx
  on public.community_fight_base (event_year);

analyze public.event_flags_mv;
analyze public.community_fight_base;

-- plpgsql + EXECUTE on purpose. As a SQL-language function (the old form) on
-- PG17 the body is planned with the parameter values unknown, which estimated
-- the filtered base at ~1 row and put the ratings aggregate on the inner side
-- of a nested loop — re-running it once per fight (35 s+). Inlining the
-- filters as literals lets the planner see the real row count and hash-join
-- (0.3-0.4 s unfiltered).
create or replace function public.community_dashboard(p_org text default null, p_wc text default null, p_year integer default null)
returns jsonb
language plpgsql stable security definer
set search_path to 'public'
as $fn$
declare
  where_sql text := 'true';
  result jsonb;
begin
  if p_org  is not null then where_sql := where_sql || format(' and b.organization = %L', p_org); end if;
  if p_wc   is not null then where_sql := where_sql || format(' and b.weight_class = %L', p_wc); end if;
  if p_year is not null then where_sql := where_sql || format(' and b.event_year = %s', p_year); end if;
  execute $q$
with f as (
  select b.*, cr.avg_rating, cr.rating_count
  from community_fight_base b
  left join (
    select fight_id, round(avg(rating),2) as avg_rating, count(*) as rating_count
    from ratings where rating is not null group by fight_id
  ) cr on cr.fight_id = b.id
  where $q$ || where_sql || $q$
),
pf as (
  select fighter1_id as fid, f1name as name, winner_id, is_dec, is_finish, is_ko, is_sub, time_sec, avg_rating from f
  union all
  select fighter2_id, f2name, winner_id, is_dec, is_finish, is_ko, is_sub, time_sec, avg_rating from f
),
fa as (
  select fid, max(name) as name,
    count(*) as fights,
    count(*) filter (where winner_id = fid) as wins,
    count(*) filter (where winner_id is not null and winner_id <> fid) as losses,
    count(*) filter (where winner_id = fid and is_dec) as decision_wins,
    count(*) filter (where winner_id = fid and is_finish) as finish_wins,
    count(*) filter (where winner_id = fid and is_ko) as ko_wins,
    count(*) filter (where winner_id = fid and is_sub) as sub_wins,
    sum(time_sec) as time_sec,
    round(avg(avg_rating) filter (where avg_rating is not null),2) as avg_rating,
    count(*) filter (where avg_rating is not null) as rated_fights
  from pf where fid is not null
  group by fid
),
ca as (
  select event_id, max(event_name) as event_name,
         round(avg(avg_rating),2) as avg_rating,
         count(avg_rating) as rated_fights
  from f
  group by event_id
  having count(avg_rating) >= 5
),
lb(key, rows) as (
  values
    ('lb_fights',    (select jsonb_agg(t) from (select fid,name,fights v from fa where fights>0 order by fights desc, name limit 10) t)),
    ('lb_wins',      (select jsonb_agg(t) from (select fid,name,wins v from fa where wins>0 order by wins desc, name limit 10) t)),
    ('lb_decisions', (select jsonb_agg(t) from (select fid,name,decision_wins v from fa where decision_wins>0 order by decision_wins desc, name limit 10) t)),
    ('lb_finishes',  (select jsonb_agg(t) from (select fid,name,finish_wins v from fa where finish_wins>0 order by finish_wins desc, name limit 10) t)),
    ('lb_ko',        (select jsonb_agg(t) from (select fid,name,ko_wins v from fa where ko_wins>0 order by ko_wins desc, name limit 10) t)),
    ('lb_sub',       (select jsonb_agg(t) from (select fid,name,sub_wins v from fa where sub_wins>0 order by sub_wins desc, name limit 10) t)),
    ('lb_time',      (select jsonb_agg(t) from (select fid,name,time_sec v from fa where time_sec>0 order by time_sec desc, name limit 10) t)),
    ('lb_losses',    (select jsonb_agg(t) from (select fid,name,losses v from fa where losses>0 order by losses desc, name limit 10) t)),
    ('lb_rating',    (select jsonb_agg(t) from (select fid,name,avg_rating v from fa where rated_fights>=3 order by avg_rating desc, name limit 10) t)),
    ('lb_rating_low',(select jsonb_agg(t) from (select fid,name,avg_rating v from fa where rated_fights>=3 order by avg_rating asc, name limit 10) t))
)
select jsonb_build_object(
  'stats', (select jsonb_build_object(
      'total_fights', count(*),
      'total_events', count(distinct event_id),
      'finishes', count(*) filter (where is_finish),
      'avg_rating', round(avg(avg_rating) filter (where avg_rating is not null),2),
      'total_time_sec', coalesce(sum(time_sec),0)
    ) from f),
  'methods', (select coalesce(jsonb_agg(jsonb_build_object('method',method,'count',c) order by c desc),'[]')
     from (select coalesce(method,'Other') method, count(*) c from f group by 1) m),
  'weight_classes', (select coalesce(jsonb_agg(jsonb_build_object('wc',wc,'count',c,'avg_rating',ar) order by c desc, wc),'[]')
     from (select weight_class wc, count(*) c, round(avg(avg_rating),2) ar from f where weight_class is not null group by 1) x),
  'top_fights', (select coalesce(jsonb_agg(jsonb_build_object('event_id',event_id,'f1',f1name,'f2',f2name,'avg_rating',avg_rating,'rating_count',rating_count)),'[]')
     from (select event_id,f1name,f2name,avg_rating,rating_count from f where rating_count>=1 order by avg_rating desc, rating_count desc limit 10) tf),
  'top_cards', (select coalesce(jsonb_agg(jsonb_build_object('event_id',event_id,'name',event_name,'avg_rating',avg_rating,'rated',rated_fights)),'[]')
     from (select * from ca order by avg_rating desc, rated_fights desc limit 10) tc),
  'low_cards', (select coalesce(jsonb_agg(jsonb_build_object('event_id',event_id,'name',event_name,'avg_rating',avg_rating,'rated',rated_fights)),'[]')
     from (select * from ca order by avg_rating asc, rated_fights desc limit 10) lc),
  'leaderboards', (select jsonb_object_agg(key, coalesce(rows,'[]')) from lb)
)
  $q$ into result;
  return result;
end;
$fn$;

-- ── my ratings ───────────────────────────────────────────────────────────────
-- security invoker: the ratings RLS policies (user_id = auth.uid()) apply, and
-- fight_search is security_invoker too. Each element is the fight_search row
-- overlaid with the ratings row (ratings keys win: id, notes), which is exactly
-- the client's old { ...fight, ...rating } merge. Newest rating first.
create or replace function public.my_ratings()
returns jsonb
language sql stable security invoker
set search_path to 'public'
as $$
  select coalesce(jsonb_agg(coalesce(to_jsonb(fs), '{}'::jsonb) || to_jsonb(r)
                            order by r.logged_at desc nulls last), '[]'::jsonb)
  from ratings r
  left join fight_search fs on fs.id = r.fight_id
  where r.user_id = auth.uid();
$$;

-- ── refresh ──────────────────────────────────────────────────────────────────
-- Called by refresh_app_caches.py at the end of every scraper pass. CONCURRENTLY
-- so readers never block (needs the unique indexes above). Service role only.
create or replace function public.refresh_app_caches()
returns void
language plpgsql security definer
set search_path to 'public'
as $$
begin
  refresh materialized view concurrently public.event_flags_mv;
  refresh materialized view concurrently public.community_fight_base;
end;
$$;

revoke execute on function public.refresh_app_caches() from public, anon, authenticated;
grant  execute on function public.refresh_app_caches() to service_role;
