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
