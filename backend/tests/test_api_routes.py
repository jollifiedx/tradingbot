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
        self.last_update: dict[str, object] = {}

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

    async def update_settings(
        self,
        *,
        updated_by: UUID,
        frozen: bool | None = None,
        buy_power_cap: Decimal | None = None,
        max_daily_loss: Decimal | None = None,
        max_per_trade_cap: Decimal | None = None,
        staleness_threshold_seconds: int | None = None,
    ) -> BotSettings:
        if self._fail:
            raise DatabaseError("boom")
        # Mirror the real helper: coalesce omitted (None) fields, attribute the
        # change, and echo back the resulting row.
        self.last_update = {
            "updated_by": updated_by,
            "frozen": frozen,
            "buy_power_cap": buy_power_cap,
            "max_daily_loss": max_daily_loss,
            "max_per_trade_cap": max_per_trade_cap,
            "staleness_threshold_seconds": staleness_threshold_seconds,
        }
        updates: dict[str, object] = {"updated_by": updated_by}
        if frozen is not None:
            updates["frozen"] = frozen
        if buy_power_cap is not None:
            updates["buy_power_cap"] = buy_power_cap
        if max_daily_loss is not None:
            updates["max_daily_loss"] = max_daily_loss
        if max_per_trade_cap is not None:
            updates["max_per_trade_cap"] = max_per_trade_cap
        if staleness_threshold_seconds is not None:
            updates["staleness_threshold_seconds"] = staleness_threshold_seconds
        return _settings().model_copy(update=updates)


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
# PATCH /settings route behaviour (auth satisfied). This is the owner's
# freeze/cap control surface -- money in/out stays Decimal-as-string.
# --------------------------------------------------------------------------


def test_patch_settings_freeze_toggle_ok_and_attributed() -> None:
    db = _FakeDB()
    client = _auth_ok(db)
    resp = client.patch("/settings", json={"frozen": False})
    assert resp.status_code == 200
    body = resp.json()
    assert body["frozen"] is False
    # updated_by is the authenticated owner id the gate returned.
    assert body["updated_by"] == str(OWNER_ID)
    # Money still serializes as string on the PATCH response (never float).
    assert isinstance(body["buy_power_cap"], str)
    # Only the field we sent was forwarded to the DB helper (coalesce the rest).
    assert db.last_update["frozen"] is False
    assert db.last_update["buy_power_cap"] is None


def test_patch_settings_cap_update_ok_money_as_string() -> None:
    client = _auth_ok(_FakeDB())
    resp = client.patch("/settings", json={"buy_power_cap": "2500.00"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["buy_power_cap"] == "2500.00"
    assert isinstance(body["buy_power_cap"], str)


def test_patch_settings_negative_cap_422() -> None:
    client = _auth_ok(_FakeDB())
    resp = client.patch("/settings", json={"buy_power_cap": "-1.00"})
    assert resp.status_code == 422


def test_patch_settings_staleness_nonpositive_422() -> None:
    client = _auth_ok(_FakeDB())
    assert (
        client.patch("/settings", json={"staleness_threshold_seconds": 0}).status_code
        == 422
    )


def test_patch_settings_empty_body_422() -> None:
    client = _auth_ok(_FakeDB())
    assert client.patch("/settings", json={}).status_code == 422


def test_patch_settings_unknown_field_422() -> None:
    # extra="forbid": a caller cannot smuggle in id/updated_by/etc.
    client = _auth_ok(_FakeDB())
    resp = client.patch("/settings", json={"frozen": True, "id": True})
    assert resp.status_code == 422


def test_patch_settings_db_failure_maps_to_503() -> None:
    client = _auth_ok(_FakeDB(fail=True))
    assert client.patch("/settings", json={"frozen": True}).status_code == 503


def test_patch_settings_money_field_is_string_in_openapi() -> None:
    # The generated TS client must treat caps as strings, never JS numbers
    # (precision loss on money). Assert the request-body schema types them
    # string, matching the GET response money fields.
    schema = app.openapi()
    body_schema = schema["paths"]["/settings"]["patch"]["requestBody"]["content"][
        "application/json"
    ]["schema"]
    ref = body_schema["$ref"].removeprefix("#/components/schemas/")
    props = schema["components"]["schemas"][ref]["properties"]
    for field in ("buy_power_cap", "max_daily_loss", "max_per_trade_cap"):
        # Optional field -> anyOf of the money type and null; the money branch
        # must be a string, never a number.
        branches = props[field]["anyOf"]
        types = {b.get("type") for b in branches}
        assert "string" in types, f"{field} must accept string money: {branches}"
        assert "number" not in types, f"{field} must not be a JS number: {branches}"


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


def test_patch_gate_no_token_401(monkeypatch: pytest.MonkeyPatch) -> None:
    # The write path (freeze/caps) must be unreachable without auth.
    client = _gated_client(monkeypatch, _FakeDB())
    assert client.patch("/settings", json={"frozen": True}).status_code == 401


def test_patch_gate_valid_but_not_owner_403(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _gated_client(monkeypatch, _FakeDB())
    resp = client.patch(
        "/settings",
        json={"frozen": True},
        headers={"Authorization": "Bearer nonowner-token"},
    )
    assert resp.status_code == 403
