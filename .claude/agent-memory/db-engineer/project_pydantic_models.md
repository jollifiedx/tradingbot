---
name: project-pydantic-models
description: Design decisions behind backend/app/core/models.py (Pydantic mirror of the Supabase schema, 2026-07-20) — read before touching models.py or writing DB-access code that consumes it
metadata:
  type: project
---

`backend/app/core/models.py` mirrors all 9 tables + the `orders_current` view from
`supabase/migrations/` 1:1 as plain Pydantic v2 models (no ORM), written once the
schema was confirmed applied+smoke-tested against the Supabase dev project
(2026-07-20). See [[project-initial-schema]] for the schema itself.

**Naming: `BotSettings`, not `Settings`.** The `settings` table (frozen flag, caps)
is modeled as `BotSettings` specifically to avoid collision with
`app.core.config.Settings` (pydantic-settings env-var config, a completely
different thing — process config vs. a live mutable DB row the worker re-reads
before every order). Any future model/table also named `settings`-adjacent should
keep this disambiguation.

**`AwareDatetime` (pydantic's built-in type), not a hand-rolled validator.** Every
`timestamptz` column is typed `pydantic.AwareDatetime` rather than `datetime` +
a custom `@field_validator` — it rejects naive datetimes out of the box
(`pydantic_core.ValidationError`, error type `timezone_aware`) and needed zero
custom code. `equity_snapshots.snapshot_date` is a plain SQL `date`, so it's
`datetime.date`, not `AwareDatetime` — don't conflate the two when adding future
date-only columns.

**Frozen (immutable) models beyond the obviously-append-only ones.** Brief
required `Decision`/`Order`/`Trade` frozen. Also froze `SettingsHistory`
(trigger-enforced append-only in SQL, identical mechanism to decisions/orders),
`AppOwner` (no app code path ever writes it — provisioned out-of-band per its own
migration comment), `LlmCall` (no DB trigger, but usage is write-once/no lifecycle
per its docstring — a judgment call, flagged in the report rather than silently
assumed), and `OrderCurrent` (read-only view, nothing ever writes through it).
Left mutable: `BotSettings` (explicitly the one live control row), `Thesis`
(outcome fields filled in post-hoc), `EquitySnapshot` (DB comment explicitly
allows same-day recompute). Freezing a Pydantic model is an app-layer-only
choice — it doesn't touch the SQL shape and needs no owner approval, unlike an
actual schema change.

**Numeric fidelity is column-by-column, not table-by-table.** Some columns in a
table have a SQL CHECK and others in the same table don't (e.g. `orders.quantity`
has `> 0` but `orders.limit_price`/`stop_price` have no check at all;
`settings_history`'s numeric columns have NO checks even though the live
`settings` table's equivalent columns do, because the CHECK constraints were only
written on `settings`). Model fields must match each column's actual SQL
constraint set exactly — don't infer or "helpfully" add a constraint (e.g.
`ge=0`) just because a sibling column has one. Reusable `Annotated[Decimal,
Field(max_digits=..., decimal_places=...)]` type aliases at the top of
models.py encode this per (precision, scale, constraint) combination — reuse
those rather than inlining new Field() calls with slightly different bounds.

**Enum values are grep-tested against the SQL, not hand-copied and trusted.**
`backend/tests/test_models.py` parses each migration file directly with a
regex (`_sql_check_in_values`) extracting the literal quoted values from
`check (<column> in (...))`, and asserts the corresponding StrEnum's values
match exactly — so if the schema changes an allowed value, model.py drifting
out of sync fails the test suite instead of silently accepting/rejecting the
wrong values.

**`theses.embedding` stays `list[float] | None` at this layer** — no pgvector
serialization/dimension validation here. That's intentionally deferred to
whichever agent builds the DB access layer, since the Python pgvector driver
choice (asyncpg + `pgvector` package vs. hand-rolled) hasn't been made yet.

**Verification for this task did include real command execution** (unlike the
file-only initial-schema task — a Supabase project was already applied by then,
but the models themselves were verified via local ruff/mypy/pytest, not by
touching the DB): `ruff check .`, `mypy app` (strict), `pytest -q` all green,
50 tests passed (1 pre-existing smoke test + 49 new in test_models.py).
