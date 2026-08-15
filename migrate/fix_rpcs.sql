-- fix_rpcs.sql — applied to prod during cutover.
-- Prod's original recalculate_debut_flags() called to_date(date, ...) assuming
-- events.date was TEXT. The migration retypes events.date to a real `date`, so
-- replace it with a date-native version. (delete_orphan_events and
-- recalculate_womens_divisions were unaffected — prod's originals kept.)

-- Same-date tie-break (2026-08-06): early UFC tournament events have a fighter
-- appearing multiple times on one card. fight_position 1 = main event, so the
-- highest fight_position is the bout fought first — that one is the debut. The
-- old tie-break was fight_id (uuid), which picked an arbitrary fight.
-- Incremental update (2026-08-15): only touch rows whose flags change — the
-- old SET ... WHERE true rewrote all 64k+ rows and hit statement timeouts.
create or replace function recalculate_debut_flags() returns void language plpgsql as $$
begin
  with f as (
    select fi.id as fight_id, fi.fighter1_id, fi.fighter2_id, fi.fight_position, e.organization, e.date
      from fights fi join events e on e.id = fi.event_id
  ),
  long as (
    select fight_id, fighter1_id as fighter_id, fight_position, organization, date from f
    union all
    select fight_id, fighter2_id, fight_position, organization, date from f
  ),
  ranked as (
    select fight_id, fighter_id, organization,
           row_number() over (partition by fighter_id, organization
                              order by date nulls last, fight_position desc nulls last, fight_id) rn
      from long
  ),
  debut as (
    select fight_id, fighter_id from ranked where rn = 1
  ),
  calc as (
    select fi.id,
           (d1.fighter_id is not null) as f1,
           (d2.fighter_id is not null) as f2
      from fights fi
      left join debut d1 on d1.fight_id = fi.id and d1.fighter_id = fi.fighter1_id
      left join debut d2 on d2.fight_id = fi.id and d2.fighter_id = fi.fighter2_id
  )
  update fights fi
     set fighter1_is_debut = c.f1,
         fighter2_is_debut = c.f2
    from calc c
   where c.id = fi.id
     and (fi.fighter1_is_debut is distinct from c.f1
       or fi.fighter2_is_debut is distinct from c.f2);
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
