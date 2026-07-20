"""Tests for the dashboard read routes and the single-owner auth gate.

Two layers:
  * Route behaviour with auth satisfied (require_owner overridden) -- exercises
    the handlers, DatabaseError->503 mapping, /account 404, and money-as-string
    JSON serialization.
  * The auth gate itself (real require_owner) -- introspection mocked via an
    httpx MockTransport, ownership via a fake DB: no token -> 401, bad token ->
    401, valid non-owner -> 403, valid owner -> 200.
"""

from __future__ import annotations

import functools
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.api.deps import get_config, get_db, require_owner
from app.api.main import app
from app.core.db import DatabaseError
from app.core.models import BotSettings, Decision, EquitySnapshot

OWNER_ID = UUID("a2bd17bf-772c-4955-82f6-53a64341a807")
OTHER_ID = uuid4()
_NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


def _settings(cap: str = "1000.50") -> BotSettings:
    return BotSettings(
        id=True,
        frozen=True,
        buy_power_cap=Decimal(cap),
        max_daily_loss=Decimal("250.00"),
        max_per_trade_cap=Decimal("500.00"),
        staleness_threshold_seconds=30,
        updated_at=_NOW,
        updated_by=OWNER_ID,
    )


def _decision() -> Decision:
    return Decision(
        id=uuid4(),
        decided_at=_NOW,
        symbol="AAPL",
        action="no_trade",
        rules_fired=[],
        created_at=_NOW,
    )


def _snapshot() -> EquitySnapshot:
    return EquitySnapshot(
        id=uuid4(),
        snapshot_date=date(2026, 7, 20),
        account_equity=Decimal("10000.00"),
        cash_balance=Decimal("10000.00"),
        buying_power=Decimal("10000.00"),
        spy_close_price=Decimal("550.00"),
        spy_benchmark_equity=Decimal("10000.00"),
        is_paper=True,
        recorded_at=_NOW,
    )


class _FakeDB:
    """Stand-in for app.core.db.Database with canned results."""

    def __init__(
        self,
        *,
        snapshot: EquitySnapshot | None = None,
        owners: tuple[UUID, ...] = (OWNER_ID,),
        fail: bool = False,
    ) -> None:
        self._snapshot = snapshot
        self._owners = owners
        self._fail = fail

    async def get_settings(self) -> BotSettings:
        if self._fail:
            raise DatabaseError("boom")
        return _settings()

    async def get_decisions(self, *, limit: int = 50, offset: int = 0) -> list[Decision]:
        if self._fail:
            raise DatabaseError("boom")
        return [_decision()]

    async def get_latest_equity_snapshot(self) -> EquitySnapshot | None:
        if self._fail:
            raise DatabaseError("boom")
        return self._snapshot

    async def is_owner(self, user_id: UUID) -> bool:
        if self._fail:
            raise DatabaseError("boom")
        return user_id in self._owners


@pytest.fixture(autouse=True)
def _clear_overrides() -> None:
    app.dependency_overrides.clear()
    yield
    app.dependency_overrides.clear()


# --------------------------------------------------------------------------
# Route behaviour (auth satisfied).
# --------------------------------------------------------------------------


def _auth_ok(db: _FakeDB) -> TestClient:
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_owner] = lambda: OWNER_ID
    return TestClient(app)


def test_get_settings_ok_and_money_is_string() -> None:
    client = _auth_ok(_FakeDB())
    resp = client.get("/settings")
    assert resp.status_code == 200
    body = resp.json()
    # Money must serialize as string, never float -- the TS client stays Decimal-safe.
    assert body["buy_power_cap"] == "1000.50"
    assert isinstance(body["buy_power_cap"], str)


def test_get_decisions_ok() -> None:
    client = _auth_ok(_FakeDB())
    resp = client.get("/decisions")
    assert resp.status_code == 200
    assert resp.json()[0]["symbol"] == "AAPL"


def test_get_account_404_when_no_snapshot() -> None:
    client = _auth_ok(_FakeDB(snapshot=None))
    assert client.get("/account").status_code == 404


def test_get_account_ok_when_snapshot_exists() -> None:
    client = _auth_ok(_FakeDB(snapshot=_snapshot()))
    resp = client.get("/account")
    assert resp.status_code == 200
    assert resp.json()["account_equity"] == "10000.00"


def test_db_failure_maps_to_503() -> None:
    client = _auth_ok(_FakeDB(fail=True))
    assert client.get("/settings").status_code == 503


# --------------------------------------------------------------------------
# The auth gate itself (real require_owner; introspection + ownership mocked).
# --------------------------------------------------------------------------


def _introspection_handler(request: httpx.Request) -> httpx.Response:
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if token == "owner-token":
        return httpx.Response(200, json={"id": str(OWNER_ID)})
    if token == "nonowner-token":
        return httpx.Response(200, json={"id": str(OTHER_ID)})
    return httpx.Response(401, json={"msg": "invalid"})


def _gated_client(monkeypatch: pytest.MonkeyPatch, db: _FakeDB) -> TestClient:
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: SimpleNamespace(
        supabase_url="https://fake.supabase.co", supabase_anon_key="anon-key"
    )
    monkeypatch.setattr(
        deps.httpx,
        "AsyncClient",
        functools.partial(httpx.AsyncClient, transport=httpx.MockTransport(_introspection_handler)),
    )
    return TestClient(app)


def test_gate_no_token_401(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _gated_client(monkeypatch, _FakeDB())
    assert client.get("/settings").status_code == 401


def test_gate_bad_token_401(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _gated_client(monkeypatch, _FakeDB())
    resp = client.get("/settings", headers={"Authorization": "Bearer garbage"})
    assert resp.status_code == 401


def test_gate_valid_but_not_owner_403(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _gated_client(monkeypatch, _FakeDB())
    resp = client.get("/settings", headers={"Authorization": "Bearer nonowner-token"})
    assert resp.status_code == 403


def test_gate_owner_200(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _gated_client(monkeypatch, _FakeDB())
    resp = client.get("/settings", headers={"Authorization": "Bearer owner-token"})
    assert resp.status_code == 200
