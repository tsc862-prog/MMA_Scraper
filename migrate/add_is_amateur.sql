-- add_is_amateur.sql — applied to prod. Distinguish amateur fights (kept in DB
-- but hidden in-app). Sherdog only marks amateur on the FIGHTER page, so:
--   1. sherdog_scraper flags whole amateur events by name ("... Amateur Series N")
--      and amateur weight classes.
--   2. amateur_scraper.py crawls fighter pages (FIGHT HISTORY - AMATEUR section)
--      to flag amateur bouts on otherwise-pro cards.

alter table fights add column if not exists is_amateur boolean not null default false;

update fights f set is_amateur = true
from events e
where f.event_id = e.id
  and (e.name ilike '%amateur%' or f.weight_class ilike '%amateur%')
  and f.is_amateur = false;

-- fight_search exposes is_amateur (appended last to preserve column order).
-- App should filter: ... where is_amateur = false  (or is not true).
