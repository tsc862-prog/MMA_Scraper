-- manual_aliases.sql — hand-resolved name mismatches for the ratings repoint.
-- These map an OLD (ufcstats) name spelling to the correct Sherdog fighter id
-- for cases the fuzzy matcher can't safely auto-resolve (both fighters' names
-- drift, leaving too little overlap). Sherdog ids are stable, so this is
-- reproducible at cutover. Apply on staging (and at cutover) BEFORE repoint.
--
-- Each entry verified by hand against the Sherdog event page.

-- ufcstats "Benardo Sopaj" -> Sherdog "Bernardo Sopai" (226661);
-- opponent also drifted ("Timmy" -> "Timothy" Cuamba), leaving only 1 shared token.
insert into fighter_aliases (alias, fighter_id) values
  ('benardo sopaj', 226661)
on conflict (alias) do update set fighter_id = excluded.fighter_id;

-- Fight Pass spells Alfred Khashakyan as "Khashakayan" (extra 'a'); alias it so
-- his VODs resolve. Sean O'Malley vs Khashakyan (DWCS S1E2, 2017) has only a
-- SnoopCast VOD (58028) which vesper won't surface by name (ranked below his UFC
-- fights), so its fightpass_url was set directly to .../video/58028.
insert into fighter_aliases (alias, fighter_id)
select 'alfred khashakayan', id from fighters where name='Alfred Khashakyan'
on conflict (alias) do nothing;
