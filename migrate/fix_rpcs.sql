-- fix_rpcs.sql — applied to prod during cutover.
-- Prod's original recalculate_debut_flags() called to_date(date, ...) assuming
-- events.date was TEXT. The migration retypes events.date to a real `date`, so
-- replace it with a date-native version. (delete_orphan_events and
-- recalculate_womens_divisions were unaffected — prod's originals kept.)

create or replace function recalculate_debut_flags() returns void language plpgsql as $$
begin
  with f as (
    select fi.id as fight_id, fi.fighter1_id, fi.fighter2_id, e.organization, e.date
      from fights fi join events e on e.id = fi.event_id
  ),
  long as (
    select fight_id, fighter1_id as fighter_id, organization, date from f
    union all
    select fight_id, fighter2_id, organization, date from f
  ),
  ranked as (
    select fight_id, fighter_id, organization,
           row_number() over (partition by fighter_id, organization
                              order by date nulls last, fight_id) rn
      from long
  )
  update fights fi set
    fighter1_is_debut = exists(select 1 from ranked r where r.fight_id=fi.id and r.fighter_id=fi.fighter1_id and r.rn=1),
    fighter2_is_debut = exists(select 1 from ranked r where r.fight_id=fi.id and r.fighter_id=fi.fighter2_id and r.rn=1)
  where true;
end; $$;

-- update_event_paramount_urls() cast the incoming id to ::UUID and compared it
-- to events.id — now bigint, giving "operator does not exist: bigint = uuid"
-- when paramount_scraper.py ran. Cast to ::bigint. (update_fight_paramount_urls
-- uses text and was unaffected; the YouTube/ESPN scrapers PATCH by id directly.)
create or replace function public.update_event_paramount_urls(data jsonb)
 returns integer language plpgsql as $function$
declare updated int;
begin
  with updates as (
    select (elem->>'id')::bigint as id, elem->>'url' as url
    from jsonb_array_elements(data) as elem
  )
  update events e set paramount_url = u.url
  from updates u where e.id = u.id;
  get diagnostics updated = row_count;
  return updated;
end;
$function$;
