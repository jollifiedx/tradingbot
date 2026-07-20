-- decisions: append-only audit log of every trading decision the rules engine makes,
-- including deliberate NO-trades (a symbol was evaluated and the engine chose not to
-- act -- that is as important to the audit trail as an actual trade).
--
-- CLAUDE.md invariant #5: never UPDATE/DELETE. Enforced via trigger (impossible for any
-- role, not just discouraged) + RLS policies that only grant SELECT/INSERT.
--
-- Runs after theses (decisions.thesis_id references theses(id)).

create table decisions (
    id uuid primary key default gen_random_uuid(),
    decided_at timestamptz not null default now(),
    symbol text not null,
    action text not null
        constraint decisions_action_valid check (
            action in ('buy', 'sell', 'hold', 'no_trade')
        ),
    rules_fired jsonb not null default '[]'::jsonb,
    llm_rationale text,
    thesis_id uuid references theses (id),
    conviction numeric(4, 3)
        constraint decisions_conviction_range check (
            conviction is null or (conviction >= 0 and conviction <= 1)
        ),
    market_data_as_of timestamptz,
    settings_snapshot jsonb,
    created_at timestamptz not null default now()
);

comment on table decisions is
    'Append-only audit log: every decision the deterministic rules engine makes, including NO-trades. Never UPDATE/DELETE -- corrections are new rows. Enforced by trigger (all roles) and RLS (authenticated/anon roles).';
comment on column decisions.action is
    'buy | sell | hold | no_trade. no_trade records that a symbol was evaluated and deliberately not traded -- absence of a row is not the same as a documented no-trade.';
comment on column decisions.rules_fired is
    'jsonb array/object describing which deterministic rule(s) triggered this decision -- the "why" from the fast path.';
comment on column decisions.llm_rationale is
    'Free-text rationale sourced from the LLM research pipeline (thesis), if this decision was influenced by one. The LLM never places orders (CLAUDE.md invariant #1) -- this column is provenance/context only.';
comment on column decisions.thesis_id is
    'Optional link back to the theses row (LLM research) that informed this decision, completing the thesis -> decision -> order -> trade audit chain.';
comment on column decisions.market_data_as_of is
    'Timestamp of the market data tick the decision was based on -- used to prove the decision was not made on stale data (CLAUDE.md invariant #3).';
comment on column decisions.settings_snapshot is
    'Snapshot of the settings row (frozen, caps) at decision time, for audit -- proves what constraints were in effect when this decision was made.';

create index decisions_symbol_idx on decisions (symbol);
create index decisions_decided_at_idx on decisions (decided_at desc);
create index decisions_thesis_id_idx on decisions (thesis_id);

create trigger decisions_prevent_update
    before update on decisions
    for each row
    execute function reject_update_or_delete();

create trigger decisions_prevent_delete
    before delete on decisions
    for each row
    execute function reject_update_or_delete();

alter table decisions enable row level security;

-- Owner (dashboard, authenticated JWT) may only ever read the audit log, never write to
-- it directly -- decisions are written exclusively by the worker via service_role, which
-- bypasses RLS. No INSERT policy is granted to `authenticated` on purpose.
create policy decisions_select_owner on decisions
    for select
    to authenticated
    using (is_app_owner());
