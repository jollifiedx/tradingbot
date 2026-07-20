---
name: project-db-access-layer
description: Design decisions behind backend/app/core/db.py (asyncpg access layer over the Supabase dev schema, 2026-07-20) — read before touching db.py or building routes/tests that use Database
metadata:
  type: project
---

`backend/app/core/db.py` is the async DB-access layer (no ORM, plain asyncpg),
built once schema + Pydantic models ([[project-initial-schema]],
[[project-pydantic-models]]) were both live. A `Database` class wraps a shared
`asyncpg.Pool`, exposing `get_settings`, `get_decisions`,
`get_latest_equity_snapshot`, `update_settings`; a `lifespan` async context
manager (exported, not yet wired into `app/api/main.py`) creates/closes the
pool. `theses`/pgvector embedding (de)serialization is explicitly deferred —
not built this milestone.

**`settings.updated_by` (and `settings_history.changed_by`, via the logging
trigger) is a real FK to `auth.users`, not a free-floating UUID.** A
fabricated `uuid4()` passed to `update_settings()` raises
`asyncpg.ForeignKeyViolationError` (`settings_updated_by_fkey`) — caught this
via a live-DB test failure. In production this is never an issue (the caller
is always an authenticated Supabase session's JWT subject, which by
definition exists in `auth.users`), but any test or script calling
`update_settings` must use a real `auth.users.id`. The dev project currently
has exactly one such row (Esther's, `agenoresther@gmail.com`) — tests fetch it
dynamically (`select id from auth.users limit 1`) rather than hardcoding the
UUID, and skip (not fail) if the environment has none. Whoever builds the
PATCH route / auth middleware should know this FK exists and needs no extra
handling (the JWT subject already satisfies it) — but a seed/bootstrap script
that tries to set `updated_by` before `app_owner`/auth is populated will hit
the same error.

**jsonb columns need an explicit codec — asyncpg does not decode them by
default.** `decisions.rules_fired` / `decisions.settings_snapshot` are jsonb;
without `conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads,
schema="pg_catalog")` registered via the pool's `init=` callback, asyncpg
hands back raw JSON text (`str`), which fails `Decision` model validation
(`list[Any] | dict[str, Any]`). Registered once in `_register_codecs`,
applied to every pooled connection. If a future table adds another
jsonb/json column, it's already covered by this same codec — no per-column
wiring needed.

**Test pattern for DB-mutating helpers: a raw `asyncpg.Connection` inside an
uncommitted transaction stands in for the `Pool` `Database` expects.**
`asyncpg.Connection` and `asyncpg.Pool` expose the identical `fetch()` /
`fetchrow()` surface `Database` calls on `self._pool`, so
`Database(conn)` (with a `# type: ignore[arg-type]` in test code, since
`Database.__init__` is typed to `asyncpg.Pool`) works at runtime with zero
changes to `db.py`. Wrapping the whole test body in
`conn.transaction()` + rollback-in-`finally` means every write (test
`decisions`/`equity_snapshots` inserts, `update_settings` calls) is fully
undone regardless of assertion outcome — used instead of a mocked pool so the
read helpers get real round-trip coverage (Decimal/AwareDatetime/UUID/jsonb
codecs all exercised for real) without ever leaving the dev DB mutated. Only
the pure error-translation path (`DatabaseError` wrapping) uses a mocked
pool (`_RaisingPool`), since provoking a real connection failure against a
healthy dev DB isn't practical.

**`asyncpg` needs the same mypy override pattern as the Webull SDK** (see
`backend/app/core/webull/client.py`'s docstring/override): no `py.typed`
marker, so `[[tool.mypy.overrides]] module = ["asyncpg.*"]
ignore_missing_imports = true` was added to `backend/pyproject.toml`, scoped
narrowly (all asyncpg access confined to `db.py`) rather than relaxing strict
mode globally.

**Verified for real against the live dev DB**, not just constructed and
assumed: ruff, `mypy app` (strict), `pytest -q` all green — 74 tests total (73
pre-existing across `test_smoke.py`/`test_models.py`/`test_webull_client.py` +
new tests in `test_db.py`, all passing). Confirmed post-suite via a direct
`asyncpg.connect` + query that `settings`/`decisions`/`equity_snapshots`/
`settings_history` row counts and the seed row's own values were byte-for-byte
identical to the pre-suite baseline — the transaction-rollback pattern above
held in practice, not just in theory.
