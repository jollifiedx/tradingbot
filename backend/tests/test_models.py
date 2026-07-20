"""Tests for app.core.models -- the Pydantic mirror of the Supabase schema.

Coverage required by the brief:
- construction happy path per model
- Decimal precision/scale is enforced (numeric(p, s) columns reject too many
  decimal places / out-of-range values)
- naive datetimes are rejected (AwareDatetime)
- StrEnum values match the SQL CHECK (... in (...)) lists byte-for-byte -- this
  test parses the migration files directly, so schema/model drift fails the
  suite rather than silently diverging.
- frozen models reject mutation
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.core.models import (
    AppOwner,
    BotSettings,
    Decision,
    DecisionAction,
    EquitySnapshot,
    LlmCall,
    LlmCallPurpose,
    Order,
    OrderCurrent,
    OrderStatus,
    OrderType,
    SettingsHistory,
    Thesis,
    Trade,
    TradeStatus,
)
from app.core.models import OrderSide as OrderSideEnum

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "supabase" / "migrations"
UTC_NOW = datetime.now(tz=UTC)


def _uuid() -> Any:
    return uuid4()


# --------------------------------------------------------------------------
# Construction happy path, one per model/table.
# --------------------------------------------------------------------------


def test_app_owner_construction() -> None:
    owner = AppOwner(id=True, user_id=_uuid(), created_at=UTC_NOW)
    assert owner.id is True


def test_bot_settings_construction() -> None:
    settings = BotSettings(
        id=True,
        frozen=True,
        buy_power_cap=Decimal("0.00"),
        max_daily_loss=Decimal("0.00"),
        max_per_trade_cap=Decimal("0.00"),
        staleness_threshold_seconds=30,
        updated_at=UTC_NOW,
        updated_by=None,
    )
    assert settings.frozen is True
    assert settings.buy_power_cap == Decimal("0.00")


def test_bot_settings_is_mutable() -> None:
    """BotSettings is the one intentionally mutable table -- assignment must work."""
    settings = BotSettings(
        id=True,
        frozen=True,
        buy_power_cap=Decimal("100.00"),
        max_daily_loss=Decimal("50.00"),
        max_per_trade_cap=Decimal("25.00"),
        staleness_threshold_seconds=30,
        updated_at=UTC_NOW,
    )
    settings.frozen = False
    assert settings.frozen is False


def test_settings_history_construction() -> None:
    row = SettingsHistory(
        id=_uuid(),
        changed_at=UTC_NOW,
        changed_by=_uuid(),
        frozen=True,
        buy_power_cap=Decimal("1000.00"),
        max_daily_loss=Decimal("200.00"),
        max_per_trade_cap=Decimal("100.00"),
        staleness_threshold_seconds=30,
    )
    assert row.frozen is True


def test_thesis_construction_with_and_without_embedding() -> None:
    thesis = Thesis(
        id=_uuid(),
        created_at=UTC_NOW,
        symbol="AAPL",
        thesis="Strong earnings momentum.",
        conviction=Decimal("0.750"),
        embedding=[0.1, 0.2, 0.3],
        model="claude-opus-4-8",
    )
    assert thesis.embedding == [0.1, 0.2, 0.3]

    thesis_no_embedding = Thesis(
        id=_uuid(),
        created_at=UTC_NOW,
        symbol="AAPL",
        thesis="Strong earnings momentum.",
        conviction=Decimal("0.750"),
        embedding=None,
        model="claude-opus-4-8",
    )
    assert thesis_no_embedding.embedding is None


def test_decision_construction_and_default_rules_fired() -> None:
    decision = Decision(
        id=_uuid(),
        decided_at=UTC_NOW,
        symbol="AAPL",
        action=DecisionAction.NO_TRADE,
        llm_rationale=None,
        thesis_id=None,
        conviction=None,
        market_data_as_of=UTC_NOW,
        settings_snapshot={"frozen": True, "buy_power_cap": "0.00"},
        created_at=UTC_NOW,
    )
    assert decision.rules_fired == []
    assert decision.action == "no_trade"


def test_order_construction() -> None:
    order = Order(
        id=_uuid(),
        client_order_id="cid-001",
        decision_id=_uuid(),
        symbol="AAPL",
        side=OrderSideEnum.BUY,
        order_type=OrderType.LIMIT,
        status=OrderStatus.PENDING,
        quantity=Decimal("10.000000"),
        limit_price=Decimal("150.0000"),
        submitted_at=UTC_NOW,
        created_at=UTC_NOW,
    )
    assert order.is_paper is True
    assert order.filled_quantity == Decimal("0")


def test_trade_construction() -> None:
    trade = Trade(
        id=_uuid(),
        symbol="AAPL",
        entry_order_id=_uuid(),
        quantity=Decimal("10.000000"),
        entry_price=Decimal("150.0000"),
        entry_at=UTC_NOW,
        created_at=UTC_NOW,
    )
    assert trade.status == TradeStatus.OPEN
    assert trade.fees == Decimal("0")


def test_equity_snapshot_construction() -> None:
    snapshot = EquitySnapshot(
        id=_uuid(),
        snapshot_date=date(2026, 7, 20),
        account_equity=Decimal("10000.00"),
        cash_balance=Decimal("5000.00"),
        buying_power=Decimal("5000.00"),
        recorded_at=UTC_NOW,
    )
    assert snapshot.snapshot_date == date(2026, 7, 20)


def test_llm_call_construction() -> None:
    call = LlmCall(
        id=_uuid(),
        called_at=UTC_NOW,
        model="claude-opus-4-8",
        purpose=LlmCallPurpose.NIGHTLY_RESEARCH,
        input_tokens=1000,
        output_tokens=500,
        cost_usd=Decimal("0.012500"),
    )
    assert call.used_batch_api is False
    assert call.cached_input_tokens == 0


def test_order_current_construction() -> None:
    view_row = OrderCurrent(
        id=_uuid(),
        client_order_id="cid-001",
        decision_id=_uuid(),
        symbol="AAPL",
        side=OrderSideEnum.BUY,
        order_type=OrderType.MARKET,
        status=OrderStatus.FILLED,
        quantity=Decimal("10.000000"),
        submitted_at=UTC_NOW,
        created_at=UTC_NOW,
        chain_root_id=_uuid(),
        chain_root_client_order_id="cid-001",
    )
    assert view_row.chain_root_client_order_id == "cid-001"


# --------------------------------------------------------------------------
# Decimal precision/scale enforcement.
# --------------------------------------------------------------------------


def test_decimal_precision_rejects_excess_decimal_places() -> None:
    with pytest.raises(ValidationError):
        Order(
            id=_uuid(),
            client_order_id="cid-002",
            decision_id=_uuid(),
            symbol="AAPL",
            side=OrderSideEnum.BUY,
            order_type=OrderType.MARKET,
            status=OrderStatus.PENDING,
            quantity=Decimal("10.1234567"),  # numeric(18,6) -- 7 decimal places
            submitted_at=UTC_NOW,
            created_at=UTC_NOW,
        )


def test_decimal_precision_rejects_negative_where_check_forbids() -> None:
    with pytest.raises(ValidationError):
        Order(
            id=_uuid(),
            client_order_id="cid-003",
            decision_id=_uuid(),
            symbol="AAPL",
            side=OrderSideEnum.BUY,
            order_type=OrderType.MARKET,
            status=OrderStatus.PENDING,
            quantity=Decimal("-1"),  # check (quantity > 0)
            submitted_at=UTC_NOW,
            created_at=UTC_NOW,
        )


def test_conviction_out_of_range_rejected() -> None:
    with pytest.raises(ValidationError):
        Thesis(
            id=_uuid(),
            created_at=UTC_NOW,
            symbol="AAPL",
            thesis="x",
            conviction=Decimal("1.5"),  # check (0 <= conviction <= 1)
            model="claude-opus-4-8",
        )


# --------------------------------------------------------------------------
# Naive datetimes rejected.
# --------------------------------------------------------------------------


def test_naive_datetime_rejected_on_decision() -> None:
    naive = datetime(2026, 7, 20, 12, 0, 0)  # noqa: DTZ001 -- deliberately naive, this is the case under test
    with pytest.raises(ValidationError):
        Decision(
            id=_uuid(),
            decided_at=naive,
            symbol="AAPL",
            action=DecisionAction.HOLD,
            created_at=UTC_NOW,
        )


def test_naive_datetime_rejected_on_order() -> None:
    naive = datetime(2026, 7, 20, 12, 0, 0)  # noqa: DTZ001 -- deliberately naive, this is the case under test
    with pytest.raises(ValidationError):
        Order(
            id=_uuid(),
            client_order_id="cid-004",
            decision_id=_uuid(),
            symbol="AAPL",
            side=OrderSideEnum.BUY,
            order_type=OrderType.MARKET,
            status=OrderStatus.PENDING,
            quantity=Decimal("1"),
            submitted_at=naive,
            created_at=UTC_NOW,
        )


def test_naive_datetime_rejected_on_equity_snapshot() -> None:
    naive = datetime(2026, 7, 20, 12, 0, 0)  # noqa: DTZ001 -- deliberately naive, this is the case under test
    with pytest.raises(ValidationError):
        EquitySnapshot(
            id=_uuid(),
            snapshot_date=date(2026, 7, 20),
            account_equity=Decimal("1.00"),
            cash_balance=Decimal("1.00"),
            buying_power=Decimal("1.00"),
            recorded_at=naive,
        )


# --------------------------------------------------------------------------
# Frozen models reject mutation.
# --------------------------------------------------------------------------


def test_frozen_models_reject_mutation() -> None:
    app_owner = AppOwner(id=True, user_id=_uuid(), created_at=UTC_NOW)
    with pytest.raises(ValidationError):
        app_owner.user_id = _uuid()  # type: ignore[misc]

    history = SettingsHistory(
        id=_uuid(),
        changed_at=UTC_NOW,
        frozen=True,
        buy_power_cap=Decimal("0.00"),
        max_daily_loss=Decimal("0.00"),
        max_per_trade_cap=Decimal("0.00"),
        staleness_threshold_seconds=30,
    )
    with pytest.raises(ValidationError):
        history.frozen = False  # type: ignore[misc]

    decision = Decision(
        id=_uuid(),
        decided_at=UTC_NOW,
        symbol="AAPL",
        action=DecisionAction.HOLD,
        created_at=UTC_NOW,
    )
    with pytest.raises(ValidationError):
        decision.action = DecisionAction.BUY  # type: ignore[misc]

    order = Order(
        id=_uuid(),
        client_order_id="cid-005",
        decision_id=_uuid(),
        symbol="AAPL",
        side=OrderSideEnum.BUY,
        order_type=OrderType.MARKET,
        status=OrderStatus.PENDING,
        quantity=Decimal("1"),
        submitted_at=UTC_NOW,
        created_at=UTC_NOW,
    )
    with pytest.raises(ValidationError):
        order.status = OrderStatus.FILLED  # type: ignore[misc]

    trade = Trade(
        id=_uuid(),
        symbol="AAPL",
        entry_order_id=_uuid(),
        quantity=Decimal("1"),
        entry_price=Decimal("1.0000"),
        entry_at=UTC_NOW,
        created_at=UTC_NOW,
    )
    with pytest.raises(ValidationError):
        trade.status = TradeStatus.CLOSED  # type: ignore[misc]

    call = LlmCall(
        id=_uuid(),
        called_at=UTC_NOW,
        model="claude-haiku-4-5",
        purpose=LlmCallPurpose.CLASSIFICATION,
        input_tokens=1,
        output_tokens=1,
        cost_usd=Decimal("0.000001"),
    )
    with pytest.raises(ValidationError):
        call.cost_usd = Decimal("1.000000")  # type: ignore[misc]

    view_row = OrderCurrent(
        id=_uuid(),
        client_order_id="cid-006",
        decision_id=_uuid(),
        symbol="AAPL",
        side=OrderSideEnum.BUY,
        order_type=OrderType.MARKET,
        status=OrderStatus.FILLED,
        quantity=Decimal("1"),
        submitted_at=UTC_NOW,
        created_at=UTC_NOW,
        chain_root_id=_uuid(),
        chain_root_client_order_id="cid-006",
    )
    with pytest.raises(ValidationError):
        view_row.status = OrderStatus.CANCELLED  # type: ignore[misc]


def test_mutable_models_permit_mutation() -> None:
    """Sanity check the inverse: tables the DB allows to be updated stay mutable."""
    thesis = Thesis(
        id=_uuid(),
        created_at=UTC_NOW,
        symbol="AAPL",
        thesis="x",
        conviction=Decimal("0.500"),
        model="claude-opus-4-8",
    )
    thesis.realized_pnl = Decimal("125.50")
    assert thesis.realized_pnl == Decimal("125.50")

    snapshot = EquitySnapshot(
        id=_uuid(),
        snapshot_date=date(2026, 7, 20),
        account_equity=Decimal("1.00"),
        cash_balance=Decimal("1.00"),
        buying_power=Decimal("1.00"),
        recorded_at=UTC_NOW,
    )
    snapshot.account_equity = Decimal("2.00")
    assert snapshot.account_equity == Decimal("2.00")


# --------------------------------------------------------------------------
# StrEnum values must match the SQL CHECK (... in (...)) lists exactly. This
# parses the migration files directly so schema/model drift fails the suite.
# --------------------------------------------------------------------------


def _read_migration(filename: str) -> str:
    path = MIGRATIONS_DIR / filename
    assert path.exists(), f"expected migration file not found: {path}"
    return path.read_text(encoding="utf-8")


def _sql_check_in_values(sql: str, column: str) -> set[str]:
    """Extract the literal string values from `check (<column> in (...))`, or
    `check (\\n <column> in (\\n 'a', 'b'\\n )\\n)`, tolerating whitespace/newlines
    but not nested parens (none of this schema's CHECKs need them)."""
    pattern = re.compile(rf"{re.escape(column)}\s+in\s*\(([^)]*)\)", re.DOTALL)
    match = pattern.search(sql)
    assert match, f"could not find `{column} in (...)` check in migration SQL"
    return set(re.findall(r"'([^']*)'", match.group(1)))


def test_decision_action_enum_matches_sql() -> None:
    sql = _read_migration("20260719000006_decisions.sql")
    assert _sql_check_in_values(sql, "action") == {member.value for member in DecisionAction}


def test_order_side_enum_matches_sql() -> None:
    sql = _read_migration("20260719000007_orders.sql")
    assert _sql_check_in_values(sql, "side") == {member.value for member in OrderSideEnum}


def test_order_type_enum_matches_sql() -> None:
    sql = _read_migration("20260719000007_orders.sql")
    assert _sql_check_in_values(sql, "order_type") == {member.value for member in OrderType}


def test_order_status_enum_matches_sql() -> None:
    sql = _read_migration("20260719000007_orders.sql")
    assert _sql_check_in_values(sql, "status") == {member.value for member in OrderStatus}


def test_trade_status_enum_matches_sql() -> None:
    sql = _read_migration("20260719000008_trades.sql")
    assert _sql_check_in_values(sql, "status") == {member.value for member in TradeStatus}


def test_llm_call_purpose_enum_matches_sql() -> None:
    sql = _read_migration("20260719000011_llm_calls.sql")
    assert _sql_check_in_values(sql, "purpose") == {member.value for member in LlmCallPurpose}
