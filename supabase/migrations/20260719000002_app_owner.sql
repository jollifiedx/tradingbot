-- Single-user allowlist.
--
-- TradingBot has exactly one legitimate dashboard user (Esther). RLS policies in this
-- schema authorize the "owner" role by checking auth.uid() against this table rather
-- than hardcoding a UUID into every policy (hardcoding would break across local/dev/prod
-- Supabase projects, each with a different auth.users row for the same person).
--
-- This table holds at most one row, enforced the same way as `settings` (see
-- 20260719000004_settings.sql for the singleton-row mechanism and its rationale).
--
-- Populated manually post-migration (`insert into app_owner (user_id) values ('<esther-auth-uid>')`)
-- once the owner's Supabase Auth account exists in a given project. Until populated, every
-- RLS policy that depends on is_app_owner() denies all authenticated access — fail closed,
-- consistent with CLAUDE.md invariant #3. The API/worker use the service_role key, which
-- bypasses RLS entirely per Supabase design, so normal backend operation does not depend on
-- this table being populated; only the dashboard's direct-to-Supabase authenticated paths do.

create table app_owner (
    id boolean primary key default true,
    user_id uuid not null references auth.users (id) on delete restrict,
    created_at timestamptz not null default now(),
    constraint app_owner_singleton check (id)
);

comment on table app_owner is
    'Singleton allowlist of exactly one auth.users.id: the sole permitted TradingBot owner. Never multi-tenant — see CLAUDE.md.';

alter table app_owner enable row level security;

-- No one may INSERT/UPDATE/DELETE this table via the API surface (anon/authenticated roles).
-- It is provisioned once via the Supabase SQL editor or service_role, by a human, out of band.
-- Authenticated users may SELECT only their own membership row, so the frontend can confirm
-- "am I the owner" without exposing the table to enumeration.
create policy app_owner_select_self on app_owner
    for select
    to authenticated
    using (user_id = auth.uid());

-- Helper used by every other table's RLS policies. SECURITY DEFINER so it can read
-- app_owner regardless of the calling role's row-level grants, while still only ever
-- returning a boolean (no data leakage).
create function is_app_owner()
returns boolean
language sql
security definer
set search_path = public
stable
as $$
    select exists (
        select 1 from app_owner where user_id = auth.uid()
    );
$$;

comment on function is_app_owner() is
    'True if the calling authenticated user is the single allowlisted TradingBot owner. Used by RLS policies across all tables.';
