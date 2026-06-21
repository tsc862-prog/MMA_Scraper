-- 03_verify.sql  —  STAGING QA gates. Run after 02_finalize.sql.
-- Every check should pass before cutover. Read the comments for pass criteria.

-- 1. Ratings are zero-loss: count unchanged (832 at 2026-06-20) and none orphaned.
select 'ratings_total' as check, count(*) as value from ratings;
select 'ratings_orphaned' as check, count(*) as value
  from ratings r left join fights f on f.id = r.fight_id
  where f.id is null;                                   -- must be 0

-- 2. No old-shape fight ids survive (ufcstats 16-hex or name-hash uuid5).
--    All new fight ids are uuid5(event_id:a:b) -> 36-char with dashes; the
--    distinguishing tell is that referenced fighter/event ids are now bigint.
select 'fights_total' as check, count(*) as value from fights;

-- 3. Referential integrity: every FK resolves (these must all be 0).
select 'fights_bad_event' as check, count(*) from fights x
  left join events e on e.id = x.event_id where e.id is null;
select 'fights_bad_f1' as check, count(*) from fights x
  left join fighters f on f.id = x.fighter1_id where f.id is null;
select 'fights_bad_f2' as check, count(*) from fights x
  left join fighters f on f.id = x.fighter2_id where f.id is null;
select 'results_bad_fight' as check, count(*) from fight_results x
  left join fights f on f.id = x.fight_id where f.id is null;
select 'events_bad_org' as check, count(*) from events x
  left join organizations o on o.id = x.organization_id where x.organization_id is not null and o.id is null;

-- 4. Id types are bigint and event date is a real date.
select column_name, data_type from information_schema.columns
 where table_schema='public'
   and ((table_name='fighters' and column_name='id')
     or (table_name='events' and column_name in ('id','date'))
     or (table_name='fights' and column_name in ('fighter1_id','event_id')));
   -- expect: fighters.id bigint, events.id bigint, events.date date,
   --         fights.fighter1_id bigint, fights.event_id bigint

-- 5. UFC title flags preserved vs the pre-migration snapshot
--    (compare this number to len(migrate/ufc_title_snapshot.json)).
select 'ufc_title_fights' as check, count(*) as value
  from fights f join events e on e.id = f.event_id
  where e.organization = 'UFC' and f.is_title;

-- 6. Spot checks — eyeball these rows.
--    (a) a title fight
select * from fight_search where is_title order by event_date desc limit 3;
--    (b) a draw / no-contest that carries a rating
select fs.* from fight_search fs join ratings r on r.fight_id = fs.id
  where fs.winner_id is null limit 5;
--    (c) a non-UFC rated fight
select fs.event_organization, fs.event_name, fs.fighter1_name, fs.fighter2_name
  from fight_search fs join ratings r on r.fight_id = fs.id
  where fs.event_organization <> 'UFC' limit 5;
--    (d) an org-spanning fighter now has ONE id (no duplicate name rows).
select name, count(*) from fighters group by name having count(*) > 1 limit 20;
   -- expect 0 rows (Sherdog ids dedupe fighters who span orgs)

-- 7. No orphan events (run RPC, then confirm).
select 'events_without_fights' as check, count(*) as value
  from events e left join fights f on f.event_id = e.id
  where f.id is null;                                   -- expect 0 after delete_orphan_events()
