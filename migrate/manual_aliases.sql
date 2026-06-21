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
