-- theses: LLM overnight research output -- the "analyst" side of the system
-- (CLAUDE.md invariant #1: LLM is analyst, not trader; this table is its only output
-- channel into the rest of the system).
--
-- Embeddings power the research-memory feedback loop: before researching a symbol again,
-- the nightly pipeline retrieves its own past theses + outcomes via similarity search, so
-- the bot "remembers" what it previously thought and how that played out.
--
-- Not append-only like decisions/orders -- theses gain outcome data (outcome_trade_id,
-- realized_pnl) once a resulting trade closes, which is an UPDATE by design, not a
-- correction of the original research. The immutable/audit-critical rows are decisions
-- and orders; theses is R&D data with a late-bound outcome column.
--
-- theses.outcome_trade_id (FK to trades.id) is added by a later migration
-- (20260719000009_theses_outcome_fk.sql) because trades does not exist yet at this point
-- in migration order -- theses -> trades -> orders -> decisions -> theses would otherwise
-- be a circular foreign key dependency across CREATE TABLE statements.

create table theses (
    id uuid primary key default gen_random_uuid(),
    created_at timestamptz not null default now(),
    symbol text not null,
    thesis text not null,
    conviction numeric(4, 3) not null
        constraint theses_conviction_range check (conviction >= 0 and conviction <= 1),
    embedding vector(1024),
    model text not null,
    -- Outcome back-link, populated when a resulting trade closes. outcome_trade_id FK
    -- added in a later migration once `trades` exists (see file header note above).
    outcome_trade_id uuid,
    realized_pnl numeric(14, 2),
    outcome_recorded_at timestamptz
);

comment on table theses is
    'LLM overnight research: symbol thesis, conviction, embedding for similarity retrieval, and outcome back-link once a resulting trade closes. The LLM never places orders -- this table is read-only input to the rules engine, joined via decisions.thesis_id.';
comment on column theses.thesis is
    'Free-text research rationale produced by the nightly LLM pipeline (claude-opus-4-8 via Batch API per tech-stack.md).';
comment on column theses.conviction is
    'LLM-assigned conviction score in [0, 1]. Advisory input to the deterministic rules engine, never a trading instruction on its own.';
comment on column theses.embedding is
    'pgvector embedding of the thesis text, dimension 1024, for similarity search against past research (the feedback-loop query: "what did I think about this symbol before, and how did it go").';
comment on column theses.model is
    'Anthropic model identifier that produced this thesis (e.g. claude-opus-4-8), for provenance and joining against llm_calls.';
comment on column theses.outcome_trade_id is
    'Back-link to the trades row that closed out a position opened as a result of this thesis, if any. FK added once trades exists.';
comment on column theses.realized_pnl is
    'Denormalized copy of the linked trade''s realized P&L, for fast thesis-quality queries without a join. Source of truth is trades.realized_pnl.';

create index theses_symbol_idx on theses (symbol);
create index theses_created_at_idx on theses (created_at desc);

-- HNSW index for cosine-similarity retrieval over the research memory. HNSW chosen over
-- IVFFlat: no training/list-count tuning needed and better recall at this table's
-- expected scale (single-user, at most a few nightly theses per symbol).
create index theses_embedding_hnsw_idx on theses
    using hnsw (embedding vector_cosine_ops);

alter table theses enable row level security;

-- theses are written by the worker's research pipeline via service_role (bypasses RLS).
-- The dashboard (authenticated owner) may only read.
create policy theses_select_owner on theses
    for select
    to authenticated
    using (is_app_owner());
