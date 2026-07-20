---
name: project-initial-schema
description: Design decisions behind the TradingBot initial Supabase schema (migrations 20260719000001-11) — read before touching settings/decisions/orders/theses/trades schema
metadata:
  type: project
---

Initial schema written 2026-07-19 as 11 migration files in `supabase/migrations/`
(`20260719000001_extensions.sql` through `20260719000011_llm_calls.sql`), file-only —
no Supabase project was linked yet, so nothing was applied or verified at runtime.

**Why 11 files instead of one:** genuine FK cycle in the natural table graph —
`theses -> trades` (outcome back-link) but `trades -> orders -> decisions -> theses`
(audit chain). Resolved by creating `theses` before `decisions`/`orders`/`trades`, then
adding `theses.outcome_trade_id`'s FK constraint in a dedicated later migration
(`20260719000009_theses_outcome_fk.sql`) once `trades` exists. Any future schema work
touching these four tables must respect this ordering or reintroduce the cycle.

**Append-only enforcement is two layers, not one:** RLS policies alone don't stop
`service_role` (which the API/worker use for all writes) or the table owner from
mutating a row — Supabase RLS is bypassed by both. `decisions` and `orders` are made
truly immutable via a `BEFORE UPDATE OR DELETE` trigger (`reject_update_or_delete()`,
defined in `20260719000003_append_only_guard.sql`) that raises unconditionally for
*every* role, plus RLS policies that grant only SELECT (no INSERT/UPDATE/DELETE policy
exists at all for `authenticated`/`anon`). Both layers exist by design — trigger is the
real enforcement, RLS is defense in depth for the dashboard's direct-to-Supabase path.

**`trades` is a third category, not append-only, not fully mutable:** a trade opens
(entry filled, exit fields NULL) and later closes (exit fields filled in once). Modeled
with a custom trigger (`guard_trade_close()`) that permits exactly the open->closed
transition and entry-side-fields-unchanged, and rejects any other UPDATE including any
change to an already-closed row. Don't reach for the generic
`reject_update_or_delete()` trigger here — trades needs its own guard function.

**`settings` is the one intentionally mutable table** (frozen flag, caps — the owner's
whole control surface is "mutate this row"). Singleton enforced via
`id boolean primary key default true` + `check (id)` (a second row would need `id =
false`, violating the check, or `id = true` again, violating the PK) — this is stronger
than an app-level singleton check. Every settings mutation is snapshotted into
`settings_history` (append-only, same trigger pattern) via an `AFTER INSERT OR UPDATE`
trigger, so the one mutable table still has a full audit trail.

**Single-user RLS pattern:** rather than hardcoding Esther's `auth.users.id` UUID into
every policy (breaks across local/dev/prod Supabase projects), added an `app_owner`
singleton-allowlist table (same singleton mechanism as settings) holding exactly one
`user_id`, and a `SECURITY DEFINER` helper function `is_app_owner()` that every other
table's RLS policies call. `app_owner` must be populated manually per-environment after
migrating (`insert into app_owner (user_id) values ('<uid>')`) — until populated, every
policy denies all authenticated access, which is fail-closed and correct, but means a
fresh environment's dashboard will show nothing until this manual step happens. Worth
flagging to whichever agent builds the dashboard auth flow or a seed/bootstrap script.

**pgvector choice:** `theses.embedding vector(1024)` with an HNSW index
(`vector_cosine_ops`), chosen over IVFFlat because it needs no list-count tuning and this
table's expected scale (single-user, nightly research) never approaches where IVFFlat's
advantages would matter.

**Not yet verified:** no Supabase project was linked at the time of writing, so none of
this was applied or smoke-tested — only manually re-read for internal consistency (FK
targets exist and are created earlier in file order, extension created before use,
trigger functions defined before their triggers). Whoever links a project and runs
`supabase db push` first should treat that as the first real verification and report
back if anything fails — HNSW index availability in particular depends on the pgvector
version bundled with the target Postgres image.

**Post-architect-review fixes (2026-07-19, still pre-apply, amended in place):** `log_settings_history()`
in `20260719000004_settings.sql` was SECURITY INVOKER, which silently broke the
authenticated owner's UPDATE path (dashboard freeze/cap changes) because the trigger's
INSERT into `settings_history` was RLS-rejected under the invoker's authenticated role
(no INSERT policy exists on `settings_history` by design). Fixed to SECURITY DEFINER +
`set search_path = public`, same pattern as `is_app_owner()`. General lesson: any
SECURITY INVOKER trigger function that writes to a *different* RLS-protected table than
the one it's attached to needs this same check — the invoker's role must actually have
an applicable INSERT/UPDATE policy on the target, or use SECURITY DEFINER. Also: added
`orders_current` view (in `20260719000007_orders.sql`, recursive CTE walking
`previous_order_id` chains to the terminal/leaf row per chain, `security_invoker = true`
so `orders_select_owner` RLS still applies) so reconciliation code doesn't have to
hand-walk chains; tightened `guard_trade_close()` in `20260719000008_trades.sql` to
actually assert `new.status = 'closed'` (previously the comment claimed this but the code
didn't enforce it — repeated no-op-ish UPDATEs to an open row would have silently
passed); and locked `is_app_owner()`'s EXECUTE grant down from PUBLIC (Postgres functions
default to PUBLIC EXECUTE, unlike tables) to `authenticated, service_role` in
`20260719000002_app_owner.sql`.

See also [[feedback-file-only-db-work]] for the workflow constraint this task was done under.
