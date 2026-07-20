---
name: feedback-file-only-db-work
description: When no Supabase project is linked yet, write migration files only — never apply, never use MCP, never assume CLI is installed
metadata:
  type: feedback
---

When asked to design/write schema before a Supabase project exists (e.g. very early
project stages), stay strictly file-only: write SQL migration files under
`supabase/migrations/` using the standard timestamp naming convention, and do NOT run
`supabase db push`, `supabase migration up`, or any Supabase CLI command, and do NOT use
the Supabase MCP server (there's nothing to point it at). Verification in this mode is
manual re-reading for internal consistency (FK targets exist and are defined earlier in
file order, extensions created before use, trigger functions defined before their
triggers) — state explicitly in the report that runtime application is unverified
pending a linked project. Confirmed this was the expected mode of work for the TradingBot
initial schema (2026-07-19) — the environment had no `psql`/`supabase`/`docker` CLI
available anyway, which matched the instruction rather than blocking it.

**Why:** TradingBot's CLAUDE.md treats schema changes as migrations-in-git applied via
CI/CLI only, never mutated directly — this is stricter than "be careful," it's the
actual workflow contract. Applying schema out-of-band (even to a scratch/local DB) would
produce drift between what's in git and what's live, which is precisely what the
migration-file convention exists to prevent.

**How to apply:** Any future TradingBot schema task should default to file-only unless
the brief explicitly says a project is linked and gives permission to apply/smoke-test.
See [[project-initial-schema]] for the schema this produced.
