# Supabase migrations

Each file is a plain SQL migration named `<YYYYMMDDHHMMSS>_<description>.sql`, the standard
Supabase CLI convention. The timestamp prefix is the sole ordering mechanism — migrations
apply in filename order, so never renumber an already-applied file; add a new one instead.

Files in this directory are applied **only** via the Supabase CLI (`supabase db push` against
a linked project, or `supabase migration up` against `supabase start` for local dev). Never
hand-apply schema changes through the Supabase MCP, dashboard SQL editor, or any other side
channel — the migration files in git are the single source of truth for schema state, and
drift between git and a live database is exactly what this convention exists to prevent.

Initial schema (`20260719000001` through `20260719000011`) is intentionally split across
several files rather than one large one, both for reviewability and because of one genuine
foreign-key ordering constraint: `theses` and `trades` reference each other indirectly
(`theses -> trades` via outcome back-link, `trades -> orders -> decisions -> theses` via the
audit chain), so `theses.outcome_trade_id`'s foreign key is added in a dedicated
later migration (`20260719000009`) once `trades` exists. See that file's header comment.

Changing the shape of `settings`, `orders`, or `decisions` requires owner (Esther) approval
per `.claude/CLAUDE.md` — treat any migration touching those tables' columns/constraints as
needing an explicit go-ahead, not just a review.
