-- Rankings snapshot history for the app's Rankings page (2026-09-06).
-- Filled by rankings_scraper.py from ufc.com and Sherdog's rankings articles.
--
-- Moments in time: every run scrapes the current lists and, when they differ
-- from the latest stored snapshot for that source, stores them under today's
-- snapshot_date. Earlier snapshots are never touched, so the table is a history
-- of every published change and the app can show the rankings "as of" any date.

create table if not exists public.rankings (
  id            bigserial primary key,
  source        text        not null,            -- 'ufc' | 'sherdog'
  snapshot_date date        not null default current_date,  -- the day this set was captured (Pacific, set by the scraper)
  division      text        not null,            -- "Heavyweight", "Women's Flyweight", "Men's Pound-for-Pound", …
  rank          integer     not null,            -- 0 = champion (UFC divisions only)
  fighter_id    bigint      references public.fighters(id) on delete set null,  -- null = not matched / not in DB
  fighter_name  text        not null,            -- as the source displays it
  previous_rank integer,                         -- from the source's movement marker; null when new or champion
  is_new        boolean     not null default false,
  record        text,                            -- Sherdog only: "14-2, 1 NC"
  promotion     text,                            -- Sherdog only: the fighter's current org
  source_url    text,                            -- the fighter's page on the source site
  scraped_at    timestamptz not null default now(),
  -- fighter_name is in the key so a published tie (ufc.com P4P had two #10s) keeps both rows
  constraint rankings_snapshot_key unique (source, snapshot_date, division, rank, fighter_name)
);

create index if not exists rankings_fighter_id_idx  on public.rankings (fighter_id);
create index if not exists rankings_source_date_idx on public.rankings (source, snapshot_date desc);

-- Read-only to the app; the scraper writes with the service key.
alter table public.rankings enable row level security;
drop policy if exists "rankings are public" on public.rankings;
create policy "rankings are public" on public.rankings
  for select to anon, authenticated using (true);

-- Latest snapshot per source: what the app shows by default.
create or replace view public.rankings_current with (security_invoker = on) as
  select r.* from public.rankings r
  where r.snapshot_date = (select max(x.snapshot_date) from public.rankings x where x.source = r.source);

-- One row per stored snapshot, for the app's "as of" date picker.
create or replace view public.rankings_snapshots with (security_invoker = on) as
  select source, snapshot_date, count(*) as entries, max(scraped_at) as scraped_at
  from public.rankings group by source, snapshot_date;
