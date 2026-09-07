# Project Rules

This file holds project-wide rules and conventions. It is loaded automatically
at the start of every Codex session, so anything here is treated as a
standing instruction. Add future rules under their own headings below.

## Documentation must stay in sync before committing

**Before committing anything, update all affected documentation in the same
commit.** When a change alters behavior, structure, schedule, schema, or
process, the docs that describe it must be updated alongside the code — never in
a later "docs" commit.

Check at minimum, and update whichever the change touches:

- `README.md` — overview / setup / usage
- `WORKFLOW.md` — the GitHub Actions nightly scraper (steps, schedule, secrets,
  tracker files). Update this whenever `.github/workflows/scrape.yml` changes —
  e.g. a step is added, removed, reordered, or its script/flags change.
- `SCHEMA.md` — database schema. Update when Supabase tables/columns change.
- `TODO.md` — running task list. Tick off or add items as work lands.
- Any script docstring whose `--flags` or behavior changed.

A commit that changes behavior but leaves its docs stale is incomplete. If you
genuinely changed nothing a doc describes, no doc update is needed — but confirm
that before committing rather than assuming.

## Future rules

Add new project rules here as separate `##` sections.
