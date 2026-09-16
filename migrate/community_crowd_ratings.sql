-- 2026-09-16: the Community dashboard reads the CROWD ratings as "other users".
--
-- The app has no real user base yet, so averaging the `ratings` table gave a
-- "community" that was just the owner's own scores. community_dashboard() now
-- takes its per-fight average and count from fight_crowd_ratings (Verdict MMA
-- fans, 0-10 halved to the app's 0-5 stars) instead of `ratings`. Only that
-- source CTE and the top-fights floor changed (>= 20 ratings so a two-vote
-- 10/10 can't top the list); everything downstream — fighter / card / division
-- averages — is unchanged. Swap the `cr` subquery back to `ratings` (or union
-- the two) when real users arrive.

CREATE OR REPLACE FUNCTION public.community_dashboard(p_org text DEFAULT NULL::text, p_wc text DEFAULT NULL::text, p_year integer DEFAULT NULL::integer)
 RETURNS jsonb
 LANGUAGE plpgsql
 STABLE SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
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
    -- "other users" = the crowd: Verdict MMA's 0-10 fan score halved to stars
    select fight_id, round(rating / 2, 2) as avg_rating, rating_count
    from fight_crowd_ratings where source = 'verdict' and rating_count > 0
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
     from (select event_id,f1name,f2name,avg_rating,rating_count from f where rating_count>=20 order by avg_rating desc, rating_count desc limit 10) tf),
  'top_cards', (select coalesce(jsonb_agg(jsonb_build_object('event_id',event_id,'name',event_name,'avg_rating',avg_rating,'rated',rated_fights)),'[]')
     from (select * from ca order by avg_rating desc, rated_fights desc limit 10) tc),
  'low_cards', (select coalesce(jsonb_agg(jsonb_build_object('event_id',event_id,'name',event_name,'avg_rating',avg_rating,'rated',rated_fights)),'[]')
     from (select * from ca order by avg_rating asc, rated_fights desc limit 10) lc),
  'leaderboards', (select jsonb_object_agg(key, coalesce(rows,'[]')) from lb)
)
  $q$ into result;
  return result;
end;
$function$;
