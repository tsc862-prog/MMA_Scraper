-- add_fighter_profile.sql — Sherdog fighter-page profile fields.
--
-- The fighter photo and bio only exist on Sherdog's FIGHTER page, which
-- sherdog_scraper.py never visits (it reads event pages only). So these columns
-- are filled by fighter_profile_scraper.py, which crawls /fighter/<id>.
--
-- No sherdog_url column: fighters.id IS the Sherdog fighter id, and
-- https://www.sherdog.com/fighter/<id> redirects to the canonical slug URL, so
-- the app builds the link from the id it already has.

alter table fighters add column if not exists image_url          text;
alter table fighters add column if not exists nationality        text;
alter table fighters add column if not exists date_of_birth      date;
alter table fighters add column if not exists height             text;         -- as displayed, e.g. 6'4"
alter table fighters add column if not exists association        text;
alter table fighters add column if not exists profile_scraped_at timestamptz;  -- null = never crawled

-- The crawler's work queue: "fighters never crawled, newest ids first".
create index if not exists fighters_profile_scraped_at_idx
  on fighters (profile_scraped_at nulls first);

-- image_url is null for fighters Sherdog has no photo for (it serves a
-- fighter_large_default.jpg placeholder) — the app falls back to initials.
-- profile_scraped_at is still stamped for those, so they aren't re-crawled.
