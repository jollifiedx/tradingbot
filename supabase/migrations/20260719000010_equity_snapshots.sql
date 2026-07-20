-- equity_snapshots: daily account value + SPY reference, driving the equity curve and
-- the SPY buy-and-hold benchmark comparison that is this project's success criterion
-- (CLAUDE.md: "beats SPY buy-and-hold in forward paper trading over a meaningful sample").
--
-- One row per trading day. Not append-only in the trigger-enforced sense -- a same-day
-- snapshot may reasonably be recomputed intraday before end-of-day close (e.g. worker
-- restarts, corrected reconciliation), so plain RLS-gated read/write via service_role is
-- sufficient here; there is no owner-facing mutation path and no audit-chain role for
-- this table (it derives from trades/reconciliation, it is not itself a source of
-- intent/reasoning).

create table equity_snapshots (
    id uuid primary key default gen_random_uuid(),
    snapshot_date date not null unique,
    account_equity numeric(16, 2) not null,
    cash_balance numeric(16, 2) not null,
    buying_power numeric(16, 2) not null,
    spy_close_price numeric(14, 4),
    spy_benchmark_equity numeric(16, 2),
    is_paper boolean not null default true,
    recorded_at timestamptz not null default now()
);

comment on table equity_snapshots is
    'One row per trading day: total account equity (Webull-reconciled) alongside an SPY buy-and-hold benchmark equity computed from the same starting capital, for the equity curve and the beats-SPY success criterion.';
comment on column equity_snapshots.account_equity is
    'Total account value (cash + positions marked to market) as reconciled against Webull at end of day.';
comment on column equity_snapshots.spy_benchmark_equity is
    'Hypothetical equity if the same starting capital had been placed in SPY on day 1 and held -- the benchmark line on the dashboard equity curve.';
comment on column equity_snapshots.is_paper is
    'TRUE for paper-environment snapshots, FALSE for live.';

create index equity_snapshots_snapshot_date_idx on equity_snapshots (snapshot_date desc);

alter table equity_snapshots enable row level security;

create policy equity_snapshots_select_owner on equity_snapshots
    for select
    to authenticated
    using (is_app_owner());
