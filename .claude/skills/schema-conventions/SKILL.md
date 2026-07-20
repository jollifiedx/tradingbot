---
name: schema-conventions
description: TradingBot schema and data-model rules — append-only audit tables, Decimal money, UTC timestamps, RLS, pgvector. Background knowledge for schema and model work.
user-invocable: false
paths:
  - "supabase/**"
  - "backend/app/core/**"
---

Standing rules for all schema and data-model work:

- `decisions` and `orders` are APPEND-ONLY audit tables: no UPDATE, no DELETE, ever —
  enforced in schema via RLS policies + triggers, not just convention. Corrections are new
  rows referencing the old one.
- Money is Postgres `numeric` / Python `Decimal` / TS string — a float touching a money
  value anywhere is a defect.
- Every timestamp is `timestamptz` stored in UTC. Naive datetimes are defects. Local time
  exists only in UI rendering.
- Every table has RLS enabled; the single-user allowlist is enforced server-side.
- `theses.embedding` is pgvector; research memory retrieval is a similarity query plus the
  back-linked outcome columns (thesis → trade → P&L).
- SQL naming: tables snake_case plural, columns snake_case, FKs `<table_singular>_id`.
- Pydantic models in `backend/app/core/models.py` mirror tables 1:1; `Decimal` fields use
  `condecimal`, never `float`.
