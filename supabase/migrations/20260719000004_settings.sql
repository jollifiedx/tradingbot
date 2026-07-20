-- settings: singleton row of live risk/control parameters.
--
-- CLAUDE.md invariant #2: the worker reads this table before EVERY order; if it's
-- unreadable, the worker halts (fail closed). CLAUDE.md invariant: bot is "born frozen" --
-- frozen defaults to TRUE so a freshly-migrated environment never trades until the owner
-- explicitly unfreezes via the dashboard.
--
-- This is NOT append-only (unlike decisions/orders) -- it is intentionally the one
-- mutable table, because the owner's whole control surface (freeze/unfreeze, caps) is
-- "mutate this row." Every mutation should still be attributable; see `settings_history`
-- below, populated by trigger, giving us an audit trail without making `settings` itself
-- append-only (which would defeat its purpose as a live control switch).

create table settings (
    id boolean primary key default true,
    frozen boolean not null default true,
    buy_power_cap numeric(14, 2) not null default 0.00,
    max_daily_loss numeric(14, 2) not null default 0.00,
    max_per_trade_cap numeric(14, 2) not null default 0.00,
    staleness_threshold_seconds integer not null default 30,
    updated_at timestamptz not null default now(),
    updated_by uuid references auth.users (id),
    constraint settings_singleton check (id),
    constraint settings_buy_power_cap_nonnegative check (buy_power_cap >= 0),
    constraint settings_max_daily_loss_nonnegative check (max_daily_loss >= 0),
    constraint settings_max_per_trade_cap_nonnegative check (max_per_trade_cap >= 0),
    constraint settings_staleness_threshold_positive check (staleness_threshold_seconds > 0)
);

comment on table settings is
    'Singleton row (id is always true) of live risk/control parameters. Bot is born frozen: frozen defaults TRUE. Worker must treat an unreadable settings row as frozen=true (fail closed) per CLAUDE.md invariant #2.';
comment on column settings.frozen is
    'Owner kill switch. TRUE = worker must not place orders. Defaults TRUE on every fresh environment.';
comment on column settings.buy_power_cap is
    'Max total capital the worker may deploy, in account currency (USD). numeric, never float.';
comment on column settings.max_daily_loss is
    'Daily realized+unrealized loss threshold; breach triggers a halt for the remainder of the trading day.';
comment on column settings.max_per_trade_cap is
    'Max capital allocated to a single position.';
comment on column settings.staleness_threshold_seconds is
    'Max seconds since last market data tick before the worker considers data stale and halts new entries.';

-- Singleton enforcement: the `check (id)` constraint plus `id boolean primary key`
-- already makes a second row (id = false, which would violate the check) or a
-- duplicate row (id = true, which would violate the primary key) impossible at the
-- constraint level -- this is the "mechanism enforcing exactly one row" required by the
-- brief, and it is stronger than an application-level check because it is enforced by
-- Postgres itself regardless of calling role.

create trigger settings_prevent_delete
    before delete on settings
    for each row
    execute function reject_update_or_delete();

comment on trigger settings_prevent_delete on settings is
    'The single settings row must never be deleted (that would leave the worker with no readable settings, which must halt-closed, not run unconfigured). Updates ARE permitted -- this is the control-plane row.';

alter table settings enable row level security;

-- Only the allowlisted owner may read or write settings via the authenticated (dashboard)
-- path. The API/worker use service_role and bypass RLS for their own reads/writes.
create policy settings_select_owner on settings
    for select
    to authenticated
    using (is_app_owner());

create policy settings_update_owner on settings
    for update
    to authenticated
    using (is_app_owner())
    with check (is_app_owner());

-- No insert/delete policies for authenticated: the singleton row is seeded by this
-- migration (below) and must never be deleted (enforced by trigger above regardless).

-- Audit trail for settings changes, since settings itself is mutable by design.
-- Append-only via the same trigger mechanism as decisions/orders.
create table settings_history (
    id uuid primary key default gen_random_uuid(),
    changed_at timestamptz not null default now(),
    changed_by uuid references auth.users (id),
    frozen boolean not null,
    buy_power_cap numeric(14, 2) not null,
    max_daily_loss numeric(14, 2) not null,
    max_per_trade_cap numeric(14, 2) not null,
    staleness_threshold_seconds integer not null
);

comment on table settings_history is
    'Append-only snapshot of every settings row state, written by trigger whenever settings changes. Gives an audit trail for the one intentionally-mutable table.';

create trigger settings_history_prevent_update
    before update on settings_history
    for each row
    execute function reject_update_or_delete();

create trigger settings_history_prevent_delete
    before delete on settings_history
    for each row
    execute function reject_update_or_delete();

alter table settings_history enable row level security;

create policy settings_history_select_owner on settings_history
    for select
    to authenticated
    using (is_app_owner());

-- Trigger function that snapshots settings into settings_history on every insert/update.
create function log_settings_history()
returns trigger
language plpgsql
as $$
begin
    insert into settings_history (
        changed_by, frozen, buy_power_cap, max_daily_loss,
        max_per_trade_cap, staleness_threshold_seconds
    ) values (
        new.updated_by, new.frozen, new.buy_power_cap, new.max_daily_loss,
        new.max_per_trade_cap, new.staleness_threshold_seconds
    );
    return new;
end;
$$;

create trigger settings_log_history
    after insert or update on settings
    for each row
    execute function log_settings_history();

-- Seed the singleton row. frozen defaults to TRUE (born frozen); all caps default to
-- 0.00 so the worker cannot deploy any capital until the owner explicitly sets caps via
-- the dashboard, on top of being frozen. Belt and suspenders.
insert into settings (id) values (true);
