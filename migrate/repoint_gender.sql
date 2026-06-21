-- repoint_gender.sql — carry fighter gender across the re-key (in-place path).
-- fighters and fighters_old are co-located, so a single UPDATE..FROM join is
-- instant (vs the REST fallback in repoint_gender.py). Apply gender only where
-- all OLD fighters sharing a name AGREE; conflicts are left for gender_scraper.
-- Run after the Sherdog rebuild, before dropping fighters_old. Then re-run
-- recalculate_womens_divisions().

update fighters f
set gender = sub.gender, gender_checked = sub.checked
from (
  select lower(name) lname, min(gender) gender, bool_or(gender_checked) checked
  from fighters_old
  where gender is not null
  group by lower(name)
  having count(distinct gender) = 1   -- unambiguous names only
) sub
where lower(f.name) = sub.lname;

select recalculate_womens_divisions();
