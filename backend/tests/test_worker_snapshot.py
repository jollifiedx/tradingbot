"""Unit tests for the worker's read-and-store equity-snapshot job.

Both collaborators are fully faked: a stand-in :class:`WebullClient` (sync
`list_accounts` / `get_account_snapshot`, an `is_live` flag) and a stand-in
:class:`Database` (async `insert_equity_snapshot` that records its kwargs). No
network, no DB, no real credentials -- `Settings` is always the injected dummy so
`load_settings()` never runs. Coverage: config-pinned vs dev-fallback account
selection, completeness / fail-closed guards, field mapping, and is_paper.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from app.core.config import Settings, WebullEnv
from app.core.models import EquitySnapshot
from app.core.webull import (
    AccountBalance,
    AccountInfo,
    AccountSnapshot,
    AccountSnapshotRequest,
)
from app.worker.snapshot import (
    SnapshotError,
    _mask_account_id,
    _resolve_account_balance,
    take_snapshot,
)

# --------------------------------------------------------------------------- #
# Fakes / builders
# --------------------------------------------------------------------------- #


def _dummy_settings(*, account_id: str | None = None) -> Settings:
    return Settings(
        webull_app_key="dummy-key",
        webull_app_secret="dummy-secret",
        webull_env=WebullEnv.PAPER,
        webull_paper_api_endpoint="api.sandbox.example.com",
        webull_account_id=account_id,
        anthropic_api_key="dummy-anthropic",
        supabase_url="https://dummy.supabase.co",
        supabase_anon_key="dummy-anon",
        supabase_service_role_key="dummy-service",
        database_url="postgresql://dummy",
    )


def _account(account_id: str, account_type: str) -> AccountInfo:
    return AccountInfo(account_id=account_id, account_type=account_type)


def _balance(
    *,
    account_id: str = "acc-1",
    net_liquidation: Decimal | None = Decimal("1000000.00"),
    total_cash: Decimal | None = Decimal("999000.00"),
    buying_power: Decimal | None = Decimal("1000000.00"),
) -> AccountBalance:
    return AccountBalance(
        account_id=account_id,
        currency="USD",
        net_liquidation=net_liquidation,
        total_cash=total_cash,
        buying_power=buying_power,
        settled_funds=Decimal("999000.00"),
    )


def _snapshot(balance: AccountBalance) -> AccountSnapshot:
    return AccountSnapshot(balance=balance, positions=(), captured_at=datetime.now(UTC))


class FakeClient:
    """Fake WebullClient: per-account snapshots keyed by account_id."""

    def __init__(
        self,
        accounts: list[AccountInfo],
        balances: dict[str, AccountBalance] | None = None,
        *,
        is_live: bool = False,
    ) -> None:
        self._accounts = accounts
        self._balances = balances or {}
        self._is_live = is_live
        self.requested_ids: list[str] = []

    @property
    def is_live(self) -> bool:
        return self._is_live

    def list_accounts(self) -> tuple[AccountInfo, ...]:
        return tuple(self._accounts)

    def get_account_snapshot(self, request: AccountSnapshotRequest) -> AccountSnapshot:
        self.requested_ids.append(request.account_id)
        return _snapshot(self._balances[request.account_id])


class FakeDB:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    async def insert_equity_snapshot(self, **kwargs: Any) -> EquitySnapshot:
        self.calls.append(kwargs)
        return EquitySnapshot(
            id=uuid4(),
            snapshot_date=kwargs.get("snapshot_date") or date(2099, 1, 1),
            account_equity=kwargs["account_equity"],
            cash_balance=kwargs["cash_balance"],
            buying_power=kwargs["buying_power"],
            is_paper=kwargs.get("is_paper", True),
            recorded_at=datetime.now(UTC),
        )

    async def close(self) -> None:
        self.closed = True


async def _run(
    client: FakeClient, db: FakeDB, *, account_id: str | None = None
) -> EquitySnapshot:
    return await take_snapshot(
        settings=_dummy_settings(account_id=account_id),
        client=client,  # type: ignore[arg-type]
        db=db,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------- #
# _mask_account_id
# --------------------------------------------------------------------------- #


def test_mask_account_id_shows_only_last_four() -> None:
    assert _mask_account_id("1234567890ABCD") == "***ABCD"
    assert _mask_account_id("AB") == "***"  # too short to reveal anything


# --------------------------------------------------------------------------- #
# _resolve_account_balance -- pinned account (production path)
# --------------------------------------------------------------------------- #


def test_resolve_pinned_uses_configured_account() -> None:
    client = FakeClient(
        accounts=[_account("acc-A", "MARGIN"), _account("acc-B", "CASH")],
        balances={"acc-A": _balance(), "acc-B": _balance()},
    )
    account, _ = _resolve_account_balance(client, "acc-A")  # type: ignore[arg-type]
    assert account.account_id == "acc-A"  # pinned wins over the CASH heuristic
    assert client.requested_ids == ["acc-A"]  # only the pinned account is read


def test_resolve_pinned_not_visible_fails_closed() -> None:
    client = FakeClient(accounts=[_account("acc-A", "CASH")], balances={"acc-A": _balance()})
    with pytest.raises(SnapshotError, match="not among the visible accounts"):
        _resolve_account_balance(client, "acc-MISSING")  # type: ignore[arg-type]


def test_resolve_pinned_incomplete_fails_closed() -> None:
    client = FakeClient(
        accounts=[_account("acc-A", "CASH")],
        balances={"acc-A": _balance(buying_power=None)},
    )
    with pytest.raises(SnapshotError, match="incomplete balance"):
        _resolve_account_balance(client, "acc-A")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# _resolve_account_balance -- dev fallback (no pin)
# --------------------------------------------------------------------------- #


def test_resolve_dev_skips_incomplete_picks_first_complete_cash() -> None:
    # Mirrors the sandbox: a CASH account with a partial balance is skipped for
    # the next complete CASH account; MARGIN accounts are ignored entirely.
    client = FakeClient(
        accounts=[
            _account("acc-margin", "MARGIN"),
            _account("acc-cash-partial", "CASH"),
            _account("acc-cash-good", "CASH"),
        ],
        balances={
            "acc-margin": _balance(),
            "acc-cash-partial": _balance(net_liquidation=None, total_cash=None),
            "acc-cash-good": _balance(),
        },
    )
    account, balance = _resolve_account_balance(client, None)  # type: ignore[arg-type]
    assert account.account_id == "acc-cash-good"
    assert balance.net_liquidation == Decimal("1000000.00")
    # never read the MARGIN account; skipped the partial CASH one
    assert client.requested_ids == ["acc-cash-partial", "acc-cash-good"]


def test_resolve_dev_no_cash_fails_closed() -> None:
    client = FakeClient(
        accounts=[_account("acc-margin", "MARGIN")],
        balances={"acc-margin": _balance()},
    )
    with pytest.raises(SnapshotError, match="no CASH account"):
        _resolve_account_balance(client, None)  # type: ignore[arg-type]


def test_resolve_dev_none_complete_fails_closed() -> None:
    client = FakeClient(
        accounts=[_account("acc-cash", "CASH")],
        balances={"acc-cash": _balance(buying_power=None)},
    )
    with pytest.raises(SnapshotError, match="no CASH account returned a complete"):
        _resolve_account_balance(client, None)  # type: ignore[arg-type]


def test_resolve_no_accounts_fails_closed() -> None:
    client = FakeClient(accounts=[], balances={})
    with pytest.raises(SnapshotError, match="no accounts"):
        _resolve_account_balance(client, None)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# take_snapshot
# --------------------------------------------------------------------------- #


async def test_take_snapshot_maps_fields_dev_fallback() -> None:
    client = FakeClient(
        accounts=[_account("acc-margin", "MARGIN"), _account("acc-cash", "CASH")],
        balances={"acc-margin": _balance(), "acc-cash": _balance()},
    )
    db = FakeDB()

    result = await _run(client, db)

    (call,) = db.calls
    # net_liquidation -> account_equity, total_cash -> cash_balance, bp -> bp
    assert call["account_equity"] == Decimal("1000000.00")
    assert call["cash_balance"] == Decimal("999000.00")
    assert call["buying_power"] == Decimal("1000000.00")
    assert call["is_paper"] is True  # derived from client.is_live (paper)
    assert result.account_equity == Decimal("1000000.00")


async def test_take_snapshot_uses_pinned_account() -> None:
    client = FakeClient(
        accounts=[_account("acc-cash", "CASH"), _account("acc-margin", "MARGIN")],
        balances={"acc-cash": _balance(), "acc-margin": _balance()},
    )
    db = FakeDB()

    await _run(client, db, account_id="acc-margin")

    # pinned account was the one read (not the first CASH heuristic)
    assert client.requested_ids == ["acc-margin"]
    assert len(db.calls) == 1


async def test_take_snapshot_is_paper_false_when_client_live() -> None:
    client = FakeClient(
        accounts=[_account("acc-cash", "CASH")],
        balances={"acc-cash": _balance()},
        is_live=True,
    )
    db = FakeDB()

    await _run(client, db)

    assert db.calls[0]["is_paper"] is False  # never hardcoded; from client.is_live


async def test_take_snapshot_incomplete_balance_fails_closed_no_write() -> None:
    client = FakeClient(
        accounts=[_account("acc-cash", "CASH")],
        balances={"acc-cash": _balance(buying_power=None)},
    )
    db = FakeDB()

    with pytest.raises(SnapshotError):
        await _run(client, db)
    assert db.calls == []  # never wrote a partial row


async def test_take_snapshot_leaves_injected_db_open() -> None:
    client = FakeClient(
        accounts=[_account("acc-cash", "CASH")],
        balances={"acc-cash": _balance()},
    )
    db = FakeDB()

    await _run(client, db)

    assert db.closed is False  # caller owns an injected db
