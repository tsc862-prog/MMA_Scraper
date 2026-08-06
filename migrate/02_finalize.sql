-- 02_finalize.sql  —  STAGING ONLY.  Run AFTER the Sherdog rebuild AND a
-- successful `repoint_ratings.py --apply` (zero unmatched).
--
-- Re-adds the foreign keys (now bigint), re-points the ratings FK at the new
-- fights, recreates the fight_search view, re-enables the audit triggers, and
-- drops the *_old snapshots. Run 03_verify.sql before declaring success.

begin;

-- 1. Re-establish foreign keys on the bigint columns.
alter table fights add constraint fights_fighter1_id_fkey
  foreign key (fighter1_id) references fighters(id);
alter table fights add constraint fights_fighter2_id_fkey
  foreign key (fighter2_id) references fighters(id);
alter table fights add constraint fights_event_id_fkey
  foreign key (event_id) references events(id);
alter table fight_results add constraint fight_results_fight_id_fkey
  foreign key (fight_id) references fights(id);
alter table fight_results add constraint fight_results_winner_id_fkey
  foreign key (winner_id) references fighters(id);
alter table fighter_aliases add constraint fighter_aliases_fighter_id_fkey
  foreign key (fighter_id) references fighters(id);
alter table events add constraint events_organization_id_fkey
  foreign key (organization_id) references organizations(id);

-- 2. Re-point ratings at the rebuilt fights (repoint already wrote new ids).
alter table ratings add constraint ratings_fight_id_fkey
  foreign key (fight_id) references fights(id);

-- 3. Recreate fight_search (matches prod as of 2026-08-06: security_invoker,
--    is_amateur, and the event-level Fight Pass URL columns).
create or replace view fight_search with (security_invoker=on) as
 select fi.id, fi.weight_class, fi.is_main, fi.is_title, fi.fight_position,
        fi.fight_position_type,
        f1.id as fighter1_id, f1.name as fighter1_name,
        f2.id as fighter2_id, f2.name as fighter2_name,
        e.id as event_id, e.name as event_name, e.date as event_date,
        e.location as event_location,
        fr.winner_id, w.name as winner_name, fr.method, fr.details, fr.round,
        fr."time",
        fi.fighter1_rank, fi.fighter2_rank, fi.notes, fi.paramount_url,
        e.paramount_url as event_paramount_url,
        fi.fighter1_is_debut, fi.fighter2_is_debut,
        e.organization as event_organization,
        fi.youtube_url, fi.fightpass_url, fi.is_amateur,
        e.fightpass_url as event_fightpass_url,
        e.fightpass_prelims_url as event_fightpass_prelims_url
   from fights fi
     join fighters f1 on f1.id = fi.fighter1_id
     join fighters f2 on f2.id = fi.fighter2_id
     join events e on e.id = fi.event_id
     left join fight_results fr on fr.fight_id = fi.id
     left join fighters w on w.id = fr.winner_id;

-- 4. Re-enable the audit triggers.
alter table events        enable trigger events_audit;
alter table fights        enable trigger fights_audit;
alter table fight_results enable trigger fight_results_audit;

-- 5. Drop the OLD snapshots.
drop table if exists fighters_old;
drop table if exists events_old;
drop table if exists fights_old;
drop table if exists fight_results_old;
drop table if exists fighter_aliases_old;

commit;

-- Finally, re-run the maintenance RPCs (idempotent):
--   select delete_orphan_events();
--   select recalculate_debut_flags();
--   select recalculate_womens_divisions();
