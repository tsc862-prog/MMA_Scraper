-- repoint_aliases.sql — carry curated fighter_aliases across the re-key.
-- The old aliases map a name variant -> an OLD (uuid) fighter id. Re-map each to
-- the NEW Sherdog fighter id by matching the old fighter's name to a new fighter.
-- Ambiguous names (shared by >1 new fighter) are skipped. Run after the rebuild,
-- before dropping *_old, and before repoint_ratings (aliases aid name matching).

insert into fighter_aliases (alias, fighter_id)
select fa.alias, nf.id
from fighter_aliases_old fa
join fighters_old fo on fo.id = fa.fighter_id
join (
  select lower(name) lname, min(id) id
  from fighters
  group by lower(name)
  having count(distinct id) = 1      -- unambiguous names only
) nf on nf.lname = lower(fo.name)
on conflict (alias) do nothing;
