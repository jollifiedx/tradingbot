-- llm_calls: cost/usage ledger for every Anthropic API call, so LLM spend is visible
-- next to trading P&L (tech-stack.md §3 / cost model). Not part of the trading audit
-- chain (decisions.llm_rationale carries the actual reasoning); this table is strictly
-- for cost accounting and optionally links back to the thesis/decision it produced.

create table llm_calls (
    id uuid primary key default gen_random_uuid(),
    called_at timestamptz not null default now(),
    model text not null,
    purpose text not null
        constraint llm_calls_purpose_valid check (
            purpose in ('nightly_research', 'intraday_summary', 'classification', 'other')
        ),
    input_tokens integer not null
        constraint llm_calls_input_tokens_nonnegative check (input_tokens >= 0),
    output_tokens integer not null
        constraint llm_calls_output_tokens_nonnegative check (output_tokens >= 0),
    cached_input_tokens integer not null default 0
        constraint llm_calls_cached_input_tokens_nonnegative check (cached_input_tokens >= 0),
    cost_usd numeric(10, 6) not null
        constraint llm_calls_cost_usd_nonnegative check (cost_usd >= 0),
    used_batch_api boolean not null default false,
    thesis_id uuid references theses (id),
    decision_id uuid references decisions (id)
);

comment on table llm_calls is
    'Cost/usage ledger for every Anthropic API call (model, tokens, cost), so LLM spend is visible next to trading P&L. Optionally links to the thesis/decision it produced.';
comment on column llm_calls.cost_usd is
    'Computed cost in USD for this call given the model''s per-token pricing at call time (including batch/caching discounts already applied). numeric, never float.';
comment on column llm_calls.used_batch_api is
    'TRUE if this call went through the Anthropic Batch API (50% discount, used for nightly research per tech-stack.md).';

create index llm_calls_called_at_idx on llm_calls (called_at desc);
create index llm_calls_thesis_id_idx on llm_calls (thesis_id);
create index llm_calls_decision_id_idx on llm_calls (decision_id);

alter table llm_calls enable row level security;

create policy llm_calls_select_owner on llm_calls
    for select
    to authenticated
    using (is_app_owner());
