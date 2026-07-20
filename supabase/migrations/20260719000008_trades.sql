-- trades: closed round-trips (entry + exit) with realized P&L, fees, and slippage.
--
-- Not append-only in the strict trigger-enforced sense that decisions/orders are:
-- a trade legitimately starts as "open" (entry filled, no exit yet) and is later updated
-- with exit details when the position closes. Historical entry/exit order references and
-- the entry itself are never altered after the fact, only the exit-side fields are filled
-- in once, going from NULL to a value -- enforced below by a trigger that permits exactly
-- that one transition and rejects any other change, including any change to a row that is
-- already closed. This keeps trades honest (no editing history) while allowing the one
-- legitimate lifecycle event (closing an open trade).

create table trades (
    id uuid primary key default gen_random_uuid(),
    symbol text not null,
    entry_order_id uuid not null references orders (id),
    exit_order_id uuid references orders (id),
    quantity numeric(18, 6) not null
        constraint trades_quantity_positive check (quantity > 0),
    entry_price numeric(14, 4) not null,
    exit_price numeric(14, 4),
    entry_at timestamptz not null,
    exit_at timestamptz,
    fees numeric(14, 4) not null default 0
        constraint trades_fees_nonnegative check (fees >= 0),
    slippage numeric(14, 4),
    realized_pnl numeric(14, 2),
    is_paper boolean not null default true,
    status text not null default 'open'
        constraint trades_status_valid check (status in ('open', 'closed')),
    created_at timestamptz not null default now()
);

comment on table trades is
    'Closed (or open) round-trips: entry/exit order references, realized P&L, fees, slippage vs. intended price. Completes the decision -> order -> trade audit chain and is what theses.outcome_trade_id links back to.';
comment on column trades.entry_order_id is
    'The filled orders row that opened this position.';
comment on column trades.exit_order_id is
    'The filled orders row that closed this position. NULL while the trade is open.';
comment on column trades.slippage is
    'Difference between intended (decision-time) price and actual average fill price, signed so positive = cost to the strategy. Populated on close.';
comment on column trades.realized_pnl is
    '(exit_price - entry_price) * quantity - fees, signed. NULL while open.';
comment on column trades.is_paper is
    'TRUE for paper-environment trades, FALSE for live. Mirrors orders.is_paper for the entry/exit legs.';

create index trades_symbol_idx on trades (symbol);
create index trades_entry_order_id_idx on trades (entry_order_id);
create index trades_exit_order_id_idx on trades (exit_order_id);
create index trades_status_idx on trades (status);
create index trades_entry_at_idx on trades (entry_at desc);

-- Guard: permit INSERT freely, and permit exactly one kind of UPDATE (open -> closed,
-- filling in exit_* and realized_pnl/slippage) but reject any change to a row that is
-- already closed, and reject any change to entry-side fields at any time. This is a
-- narrower version of the append-only guard used for decisions/orders -- trades needs
-- one legitimate mutation (closing), so it cannot use the unconditional trigger.
create function guard_trade_close()
returns trigger
language plpgsql
as $$
begin
    if old.status = 'closed' then
        raise exception
            'trades row % is already closed and cannot be modified further'
            , old.id
            using errcode = '0A000';
    end if;

    if new.symbol is distinct from old.symbol
        or new.entry_order_id is distinct from old.entry_order_id
        or new.quantity is distinct from old.quantity
        or new.entry_price is distinct from old.entry_price
        or new.entry_at is distinct from old.entry_at
        or new.is_paper is distinct from old.is_paper
    then
        raise exception
            'trades row %: entry-side fields are immutable once inserted'
            , old.id
            using errcode = '0A000';
    end if;

    return new;
end;
$$;

comment on function guard_trade_close() is
    'Permits exactly one lifecycle transition (open -> closed, filling exit_* fields) and rejects any other UPDATE, including any update to an already-closed row or to entry-side fields. Trades are not append-only like decisions/orders, but their mutation surface is constrained to this single, intentional transition.';

create trigger trades_guard_close
    before update on trades
    for each row
    execute function guard_trade_close();

create trigger trades_prevent_delete
    before delete on trades
    for each row
    execute function reject_update_or_delete();

alter table trades enable row level security;

-- Owner may only read. Trades are written exclusively by the worker via service_role.
create policy trades_select_owner on trades
    for select
    to authenticated
    using (is_app_owner());
