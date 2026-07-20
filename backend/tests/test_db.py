"""Tests for app.core.db.

Two layers, per the brief:

- **Live dev-DB integration tests** for the read/update helpers round-tripping
  into `app.core.models` (the DB is live and reachable via `DATABASE_URL` in
  `backend/.env`). Every test that writes anything runs inside a single
  `asyncpg` transaction that is rolled back in fixture teardown (`tx`
  fixture below), so this suite never leaves the dev `settings` singleton (or
  any other table) actually mutated -- satisfies the brief's "test inside a
  transaction that is rolled back" option. Any row inserted for a read test
  (decisions, equity_snapshots) uses obviously-synthetic identifiers
  (`ZTEST_*` symbols, year-2099 snapshot dates) and is rolled back the same
  way -- never committed, never resembling a real trade.
- A couple of **mocked-pool unit tests** for the DatabaseError-translation
  path (`_RaisingPool`), since provoking a real connection/query failure
  against a live, healthy dev DB isn't practical.
"""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import asyncpg
import pytest

from app.core.config import load_settings
from app.core.db import Database, DatabaseError, _register_codecs
from app.core.models import BotSettings, Decision, EquitySnapshot

# --------------------------------------------------------------------------
# Live dev-DB fixture: one connection, one uncommitted transaction per test.
# --------------------------------------------------------------------------


@dataclasses.dataclass
class _Tx:
    db: Database
    conn: asyncpg.Connection


@pytest.fixture
async def tx() -> AsyncIterator[_Tx]:
    database_url = load_settings().database_url
    conn = await asyncpg.connect(dsn=database_url)
    await _register_codecs(conn)
    transaction = conn.transaction()
    await transaction.start()
    try:
        # asyncpg.Connection exposes the same fetch()/fetchrow() surface
        # Database calls on self._pool -- standing in for a Pool here needs
        # no change to app.core.db, and confines every write below to this
        # one uncommitted transaction.
        yield _Tx(db=Database(conn), conn=conn)  # type: ignore[arg-type]
    finally:
        await transaction.rollback()
        await conn.close()


# --------------------------------------------------------------------------
# get_settings
# --------------------------------------------------------------------------


async def test_get_settings_reads_singleton(tx: _Tx) -> None:
    result = await tx.db.get_settings()
    assert isinstance(result, BotSettings)
    assert result.id is True
    assert isinstance(result.frozen, bool)
    assert isinstance(result.buy_power_cap, Decimal)
    assert result.buy_power_cap >= 0
    assert result.staleness_threshold_seconds > 0
    assert result.updated_at.tzinfo is not None


# --------------------------------------------------------------------------
# get_decisions
# --------------------------------------------------------------------------


async def test_get_decisions_orders_newest_first_and_decodes_jsonb(tx: _Tx) -> None:
    older = datetime(2026, 1, 1, tzinfo=UTC)
    newer = datetime(2026, 1, 2, tzinfo=UTC)
    await tx.conn.execute(
        """
        insert into decisions (decided_at, symbol, action, rules_fired, settings_snapshot)
        values ($1, $2, $3, $4, $5)
        """,
        older,
        "ZTEST_OLDER",
        "no_trade",
        [{"rule": "synthetic-older"}],
        {"frozen": True},
    )
    await tx.conn.execute(
        """
        insert into decisions (decided_at, symbol, action, rules_fired)
        values ($1, $2, $3, $4)
        """,
        newer,
        "ZTEST_NEWER",
        "no_trade",
        [{"rule": "synthetic-newer"}],
    )

    results = await tx.db.get_decisions(limit=10)

    assert all(isinstance(d, Decision) for d in results)
    ours = [d for d in results if d.symbol in ("ZTEST_OLDER", "ZTEST_NEWER")]
    assert [d.symbol for d in ours] == ["ZTEST_NEWER", "ZTEST_OLDER"]  # desc order
    newest = ours[0]
    assert newest.rules_fired == [{"rule": "synthetic-newer"}]
    oldest = ours[1]
    assert oldest.settings_snapshot == {"frozen": True}
    assert oldest.decided_at.tzinfo is not None


async def test_get_decisions_no_matching_rows_returns_empty_list(tx: _Tx) -> None:
    results = await tx.db.get_decisions(limit=1000, offset=1_000_000)
    assert results == []


async def test_get_decisions_rejects_bad_limit_and_offset(tx: _Tx) -> None:
    with pytest.raises(ValueError, match="limit"):
        await tx.db.get_decisions(limit=0)
    with pytest.raises(ValueError, match="offset"):
        await tx.db.get_decisions(offset=-1)


# --------------------------------------------------------------------------
# get_latest_equity_snapshot
# --------------------------------------------------------------------------


async def test_get_latest_equity_snapshot_returns_most_recent(tx: _Tx) -> None:
    await tx.conn.execute(
        """
        insert into equity_snapshots (snapshot_date, account_equity, cash_balance, buying_power)
        values ($1, $2, $3, $4)
        """,
        date(2099, 1, 1),
        Decimal("1000.00"),
        Decimal("500.00"),
        Decimal("500.00"),
    )
    await tx.conn.execute(
        """
        insert into equity_snapshots (snapshot_date, account_equity, cash_balance, buying_power)
        values ($1, $2, $3, $4)
        """,
        date(2099, 1, 2),
        Decimal("1050.00"),
        Decimal("550.00"),
        Decimal("500.00"),
    )

    latest = await tx.db.get_latest_equity_snapshot()

    assert isinstance(latest, EquitySnapshot)
    assert latest.snapshot_date == date(2099, 1, 2)
    assert latest.account_equity == Decimal("1050.00")


async def test_get_latest_equity_snapshot_none_when_table_empty(tx: _Tx) -> None:
    await tx.conn.execute("delete from equity_snapshots")  # scoped to this rolled-back tx
    result = await tx.db.get_latest_equity_snapshot()
    assert result is None


# --------------------------------------------------------------------------
# update_settings
# --------------------------------------------------------------------------


async def test_update_settings_updates_given_fields_and_logs_history(tx: _Tx) -> None:
    # settings.updated_by (and settings_history.changed_by, via trigger) is a
    # real FK to auth.users -- in production this is always satisfied because
    # the caller is an authenticated Supabase session's JWT subject. A
    # fabricated uuid4() would violate settings_updated_by_fkey, so pull the
    # one real owner row this dev project already has (see CLAUDE.md: Esther
    # is sole user) rather than hardcoding her UUID.
    actor = await tx.conn.fetchval("select id from auth.users limit 1")
    if actor is None:
        pytest.skip("no auth.users row in this environment to satisfy the updated_by FK")

    before = await tx.db.get_settings()
    new_threshold = before.staleness_threshold_seconds + 1

    updated = await tx.db.update_settings(
        updated_by=actor,
        staleness_threshold_seconds=new_threshold,
    )

    assert isinstance(updated, BotSettings)
    assert updated.staleness_threshold_seconds == new_threshold
    assert updated.updated_by == actor
    # omitted fields are left unchanged (SQL coalesce), never overwritten with None
    assert updated.frozen == before.frozen
    assert updated.buy_power_cap == before.buy_power_cap
    assert updated.max_daily_loss == before.max_daily_loss
    assert updated.max_per_trade_cap == before.max_per_trade_cap

    history_count = await tx.conn.fetchval(
        "select count(*) from settings_history where changed_by = $1", actor
    )
    assert history_count == 1


async def test_update_settings_requires_at_least_one_field(tx: _Tx) -> None:
    with pytest.raises(ValueError, match="at least one field"):
        await tx.db.update_settings(updated_by=uuid4())


# --------------------------------------------------------------------------
# DatabaseError translation (mocked pool -- no DB needed).
# --------------------------------------------------------------------------


class _RaisingPool:
    """Stands in for asyncpg.Pool; every call raises a PostgresError."""

    async def fetchrow(self, *args: Any, **kwargs: Any) -> Any:
        raise asyncpg.PostgresError("simulated connection failure")

    async def fetch(self, *args: Any, **kwargs: Any) -> Any:
        raise asyncpg.PostgresError("simulated connection failure")


async def test_get_settings_wraps_failure_as_database_error() -> None:
    db = Database(_RaisingPool())  # type: ignore[arg-type]
    with pytest.raises(DatabaseError):
        await db.get_settings()


async def test_get_decisions_wraps_failure_as_database_error() -> None:
    db = Database(_RaisingPool())  # type: ignore[arg-type]
    with pytest.raises(DatabaseError):
        await db.get_decisions()


async def test_get_latest_equity_snapshot_wraps_failure_as_database_error() -> None:
    db = Database(_RaisingPool())  # type: ignore[arg-type]
    with pytest.raises(DatabaseError):
        await db.get_latest_equity_snapshot()


async def test_update_settings_wraps_failure_as_database_error() -> None:
    db = Database(_RaisingPool())  # type: ignore[arg-type]
    with pytest.raises(DatabaseError):
        await db.update_settings(updated_by=uuid4(), frozen=True)
