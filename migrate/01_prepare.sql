-- 01_prepare.sql  —  STAGING ONLY.  Run after restoring a prod pg_dump.
--
-- Prepares staging for the Sherdog re-key: snapshots the OLD data (so
-- repoint_ratings.py can map old rating ids), then rebuilds the real tables
-- empty with Sherdog-shaped ids (uuid -> bigint). Triggers, the fight_search
-- view, *_url columns, RLS and the RPCs are all preserved (carried by pg_dump).
--
-- `ratings` is NOT truncated — its rows are re-pointed in place later.
-- Schema confirmed against prod 2026-06-20: fighters.id/events.id are uuid,
-- fights.id is text, events.date is text.

begin;

-- 0. Snapshot OLD data for the repoint joins (plain copies, no constraints).
create table fighters_old        as select * from fighters;
create table events_old          as select * from events;
create table fights_old          as select * from fights;
create table fight_results_old   as select * from fight_results;
create table fighter_aliases_old as select * from fighter_aliases;

-- 1. Drop the view that depends on the columns we're about to retype.
drop view if exists fight_search;

-- 2. Drop every FK touching the id columns whose type changes (uuid -> bigint),
--    plus the ratings FK so ratings survive while fights is rebuilt.
alter table fights         drop constraint if exists fights_fighter1_id_fkey;
alter table fights         drop constraint if exists fights_fighter2_id_fkey;
alter table fights         drop constraint if exists fights_event_id_fkey;
alter table fight_results  drop constraint if exists fight_results_fight_id_fkey;
alter table fight_results  drop constraint if exists fight_results_winner_id_fkey;
alter table fighter_aliases drop constraint if exists fighter_aliases_fighter_id_fkey;
alter table ratings        drop constraint if exists ratings_fight_id_fkey;

-- 3. Silence the audit triggers during the bulk rebuild (avoid change_log bloat).
alter table events        disable trigger events_audit;
alter table fights        disable trigger fights_audit;
alter table fight_results disable trigger fight_results_audit;

-- 4. Empty the data tables (NOT ratings).
truncate fighters, events, fights, fight_results, fighter_aliases;

-- 5. Re-type ids to Sherdog bigint (tables are empty, so USING NULL is safe).
alter table fighters        alter column id drop default;
alter table fighters        alter column id type bigint using null;
alter table fighter_aliases alter column fighter_id type bigint using null;

alter table events          alter column id drop default;
alter table events          alter column id type bigint using null;
alter table events          alter column date type date using null;   -- text -> date

alter table fights          alter column fighter1_id type bigint using null;
alter table fights          alter column fighter2_id type bigint using null;
alter table fights          alter column event_id   type bigint using null;
-- fights.id stays text (new deterministic hash of event_id + sorted fighter ids).

alter table fight_results   alter column winner_id  type bigint using null;
-- fight_results.fight_id stays text.

-- 5b. Drop the fighters.name UNIQUE constraint. Under name-hash ids each name
--     mapped to one row; with Sherdog ids, different people who share a name
--     (e.g. multiple "Bruno Silva") are correctly DISTINCT rows.
alter table fighters drop constraint if exists fighters_name_key;

-- 6. organizations lookup + events FK target (sherdog_scraper writes both
--    events.organization text and events.organization_id).
create table if not exists organizations (
  id     bigint primary key,
  name   text not null,
  status text
);
alter table events add column if not exists organization_id bigint;

commit;

-- Next: point .env at STAGING and run
--   python sherdog_scraper.py --all          (full rebuild incl. UFC)
--   python migrate/repoint_ratings.py        (dry-run, must be 0 unmatched)
--   python migrate/repoint_ratings.py --apply
-- then run 02_finalize.sql.
