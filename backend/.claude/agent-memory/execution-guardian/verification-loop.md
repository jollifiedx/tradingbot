---
name: verification-loop
description: How safety work gets verified in this repo — venv-scoped pytest/ruff/mypy, and a LIVE dev Supabase DB that DB-layer tests hit inside rolled-back transactions
metadata:
  type: project
---

Backend checks run through the repo venv, not the system Python: `./.venv/Scripts/python.exe -m pytest -q`,
`... -m ruff check app tests`, `... -m mypy --strict app` (bare `python -m pytest` fails — pytest is not
installed system-wide).

`tests/test_db.py` runs against the **live Supabase dev DB** (`DATABASE_URL` in `backend/.env`), each test
inside one asyncpg transaction that is rolled back in fixture teardown. Synthetic rows use `ZTEST_*` symbols
and year-2099 dates.

**Why:** a mocked DB would let a query that is wrong against the real schema (append-only triggers, FK chain
decisions→orders→trades, NOT NULL money columns) pass tests and fail in the money path. The rollback fixture
is what makes hitting the real DB safe.

**How to apply:** when a safety change needs a new DB read, add the helper to `app/core/db.py` and test it
live via the `tx` fixture (insert a synthetic decision→order→trade chain if the query touches `trades`) rather
than faking the pool. Keep the mocked-`_RaisingPool` unit test too — it is the only practical way to prove the
DatabaseError/fail-closed translation. Related: [[reconciliation-open-items]].
