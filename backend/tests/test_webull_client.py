"""Unit tests for the read-only Webull client wrapper.

The SDK is fully mocked: the underlying ``TradeClient`` / ``DataClient`` are
replaced with fakes, so no test touches the network or needs real credentials.
``Settings`` is always constructed with explicit dummy values, so the suite is
green with an empty environment and never reads real keys.

Coverage per public method: happy path + timeout + malformed response + auth
failure, plus exception-translation and safety-posture checks.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest

# Importing the SDK's exception types here (test-only) to simulate raw SDK
# failures. Production code confines SDK imports to app/core/webull/client.py.
from webull.core.exception import error_code
from webull.core.exception.exceptions import ClientException, ServerException

from app.core.config import Settings, WebullEnv
from app.core.webull import (
    AccountSnapshotRequest,
    BarTimespan,
    HistoricalBarsRequest,
    OrderStatus,
    OrderStatusRequest,
    WebullAPIError,
    WebullAuthError,
    WebullClient,
    WebullError,
    WebullMalformedResponseError,
    WebullRateLimitError,
    WebullTimeoutError,
)

# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #


def _dummy_settings(env: WebullEnv = WebullEnv.PAPER) -> Settings:
    """Settings with all-dummy values; explicit kwargs override any .env file."""
    return Settings(
        webull_app_key="dummy-key",
        webull_app_secret="dummy-secret",
        webull_env=env,
        anthropic_api_key="dummy-anthropic",
        supabase_url="https://dummy.supabase.co",
        supabase_anon_key="dummy-anon",
        supabase_service_role_key="dummy-service",
        database_url="postgresql://dummy",
    )


class FakeResponse:
    """Mimics the ``requests.Response`` object the SDK returns."""

    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class BadJsonResponse:
    """A response whose body is not decodable JSON."""

    def json(self) -> Any:
        raise ValueError("body is not JSON")


class _Namespace:
    """Attribute bag for building fake trade/data client sub-objects."""

    def __init__(self, **attrs: Any) -> None:
        self.__dict__.update(attrs)


class CountingLimiter:
    """Rate-limiter double that records how many times acquire() ran."""

    def __init__(self) -> None:
        self.calls = 0

    def acquire(self) -> None:
        self.calls += 1


def _make_client(
    *,
    account_fns: dict[str, Any] | None = None,
    order_fns: dict[str, Any] | None = None,
    market_fns: dict[str, Any] | None = None,
    env: WebullEnv = WebullEnv.PAPER,
    limiter: CountingLimiter | None = None,
) -> WebullClient:
    """Build a WebullClient with injected fake trade/data clients (no SDK build)."""
    client = WebullClient(_dummy_settings(env), rate_limiter=limiter)  # type: ignore[arg-type]
    fake_trade = _Namespace(
        account=_Namespace(**(account_fns or {})),
        order=_Namespace(**(order_fns or {})),
    )
    fake_data = _Namespace(market_data=_Namespace(**(market_fns or {})))
    # Pre-seed both so _ensure_clients() short-circuits and never builds the SDK.
    client._trade_client = fake_trade
    client._data_client = fake_data
    return client


def _raises(exc: BaseException) -> Any:
    def _fn(*_args: Any, **_kwargs: Any) -> Any:
        raise exc

    return _fn


# --------------------------------------------------------------------------- #
# Construction / environment routing
# --------------------------------------------------------------------------- #


def test_env_defaults_to_paper() -> None:
    client = WebullClient(_dummy_settings())
    assert client.env is WebullEnv.PAPER
    assert client.is_live is False


def test_env_live_is_reported_from_settings_not_hardcoded() -> None:
    client = WebullClient(_dummy_settings(env=WebullEnv.LIVE))
    assert client.env is WebullEnv.LIVE
    assert client.is_live is True


def test_wrapper_exposes_no_order_mutating_methods() -> None:
    # Safety: this read-only wrapper must never grow place/modify/cancel methods.
    for forbidden in ("place_order", "replace_order", "cancel_order", "submit_order"):
        assert not hasattr(WebullClient, forbidden)


# --------------------------------------------------------------------------- #
# Account snapshot
# --------------------------------------------------------------------------- #

_BALANCE_OK = {
    "account_id": "ACC1",
    "currency": "USD",
    "net_liquidation_value": "10000.50",
    "total_cash_value": "2500.25",
    "buying_power": "5000.00",
    "settled_funds": "2500.25",
}

_POSITIONS_OK = [
    {
        "instrument_id": "913256135",
        "symbol": "AAPL",
        "quantity": "10",
        "cost_price": "150.10",
        "market_value": "1600.00",
        "unrealized_profit_loss": "98.99",
    }
]


def test_account_snapshot_happy_path() -> None:
    limiter = CountingLimiter()
    client = _make_client(
        account_fns={
            "get_account_balance": lambda *_: FakeResponse(_BALANCE_OK),
            "get_account_position": lambda *_: FakeResponse(_POSITIONS_OK),
        },
        limiter=limiter,
    )
    snap = client.get_account_snapshot(AccountSnapshotRequest(account_id="ACC1"))

    assert snap.balance.account_id == "ACC1"
    assert snap.balance.total_cash == Decimal("2500.25")
    assert isinstance(snap.balance.buying_power, Decimal)
    assert len(snap.positions) == 1
    pos = snap.positions[0]
    assert pos.symbol == "AAPL"
    assert pos.quantity == Decimal("10")
    assert isinstance(pos.market_value, Decimal)
    # captured_at is tz-aware UTC
    assert snap.captured_at.tzinfo is not None
    assert snap.captured_at.utcoffset() == timedelta(0)
    # rate limiter engaged for every underlying call (balance + 1 positions page)
    assert limiter.calls == 2


def test_account_snapshot_paginates_positions() -> None:
    pages = [
        FakeResponse(
            [
                {"instrument_id": "1", "symbol": "AAA", "quantity": "1"},
                {"instrument_id": "2", "symbol": "BBB", "quantity": "2"},
            ]
        ),
        FakeResponse(
            [
                {"instrument_id": "3", "symbol": "CCC", "quantity": "3"},
                {"instrument_id": "4", "symbol": "DDD", "quantity": "4"},
            ]
        ),
        FakeResponse([{"instrument_id": "5", "symbol": "EEE", "quantity": "5"}]),
    ]
    call_log: list[Any] = []

    def _positions(account_id: str, page_size: int, last_id: Any) -> Any:
        call_log.append(last_id)
        return pages[len(call_log) - 1]

    client = _make_client(
        account_fns={
            "get_account_balance": lambda *_: FakeResponse(_BALANCE_OK),
            "get_account_position": _positions,
        },
    )
    req = AccountSnapshotRequest(account_id="ACC1", page_size=2, max_pages=10)
    snap = client.get_account_snapshot(req)

    assert [p.symbol for p in snap.positions] == ["AAA", "BBB", "CCC", "DDD", "EEE"]
    # First page starts with no cursor, then walks by last instrument id.
    assert call_log == [None, "2", "4"]


def test_account_snapshot_auth_failure() -> None:
    client = _make_client(
        account_fns={
            "get_account_balance": _raises(
                ServerException("AUTH", "unauthorized", http_status=401)
            ),
            "get_account_position": lambda *_: FakeResponse([]),
        },
    )
    with pytest.raises(WebullAuthError):
        client.get_account_snapshot(AccountSnapshotRequest(account_id="ACC1"))


def test_account_snapshot_timeout() -> None:
    client = _make_client(
        account_fns={
            "get_account_balance": _raises(
                ClientException(error_code.SDK_HTTP_ERROR, "HTTPSConnectionPool: Read timed out")
            ),
            "get_account_position": lambda *_: FakeResponse([]),
        },
    )
    with pytest.raises(WebullTimeoutError):
        client.get_account_snapshot(AccountSnapshotRequest(account_id="ACC1"))


def test_account_snapshot_malformed_balance() -> None:
    client = _make_client(
        account_fns={
            "get_account_balance": lambda *_: BadJsonResponse(),
            "get_account_position": lambda *_: FakeResponse([]),
        },
    )
    with pytest.raises(WebullMalformedResponseError):
        client.get_account_snapshot(AccountSnapshotRequest(account_id="ACC1"))


def test_account_snapshot_malformed_position_value() -> None:
    client = _make_client(
        account_fns={
            "get_account_balance": lambda *_: FakeResponse(_BALANCE_OK),
            "get_account_position": lambda *_: FakeResponse(
                [{"instrument_id": "1", "symbol": "AAA", "quantity": "not-a-number"}]
            ),
        },
    )
    with pytest.raises(WebullMalformedResponseError):
        client.get_account_snapshot(AccountSnapshotRequest(account_id="ACC1"))


# --------------------------------------------------------------------------- #
# Historical bars
# --------------------------------------------------------------------------- #

_BARS_NEWEST_FIRST = [
    {
        "timeStamp": "1710003600",
        "open": "101.0",
        "high": "102.0",
        "low": "100.5",
        "close": "101.5",
        "volume": "2000",
        "vwap": "101.2",
    },
    {
        "timeStamp": "1710000000",
        "open": "100.0",
        "high": "101.0",
        "low": "99.5",
        "close": "100.5",
        "volume": "1000",
    },
]


def test_historical_bars_happy_path_sorted_and_typed() -> None:
    client = _make_client(
        market_fns={"get_history_bar": lambda *_: FakeResponse(_BARS_NEWEST_FIRST)},
    )
    result = client.get_historical_bars(
        HistoricalBarsRequest(symbol="AAPL", timespan=BarTimespan.DAY, count=2)
    )
    assert result.symbol == "AAPL"
    assert len(result.bars) == 2
    # normalised oldest -> newest despite newest-first input
    assert result.bars[0].timestamp < result.bars[1].timestamp
    first = result.bars[0]
    assert first.open == Decimal("100.0")
    assert isinstance(first.close, Decimal)
    assert first.vwap is None  # missing vwap tolerated
    assert first.timestamp.tzinfo is not None
    assert first.timestamp.utcoffset() == timedelta(0)


def test_historical_bars_accepts_dict_envelope() -> None:
    client = _make_client(
        market_fns={
            "get_history_bar": lambda *_: FakeResponse(
                {"symbol": "AAPL", "bars": _BARS_NEWEST_FIRST}
            )
        },
    )
    result = client.get_historical_bars(
        HistoricalBarsRequest(symbol="AAPL", timespan=BarTimespan.MIN_60)
    )
    assert len(result.bars) == 2


def test_historical_bars_timeout() -> None:
    client = _make_client(
        market_fns={
            "get_history_bar": _raises(
                ClientException(error_code.SDK_HTTP_ERROR, "connection timed out")
            )
        },
    )
    with pytest.raises(WebullTimeoutError):
        client.get_historical_bars(
            HistoricalBarsRequest(symbol="AAPL", timespan=BarTimespan.DAY)
        )


def test_historical_bars_auth_failure() -> None:
    client = _make_client(
        market_fns={
            "get_history_bar": _raises(ServerException("AUTH", "forbidden", http_status=403))
        },
    )
    with pytest.raises(WebullAuthError):
        client.get_historical_bars(
            HistoricalBarsRequest(symbol="AAPL", timespan=BarTimespan.DAY)
        )


def test_historical_bars_malformed_missing_timestamp() -> None:
    client = _make_client(
        market_fns={
            "get_history_bar": lambda *_: FakeResponse(
                [{"open": "1", "high": "1", "low": "1", "close": "1", "volume": "1"}]
            )
        },
    )
    with pytest.raises(WebullMalformedResponseError):
        client.get_historical_bars(
            HistoricalBarsRequest(symbol="AAPL", timespan=BarTimespan.DAY)
        )


def test_historical_bars_malformed_non_json() -> None:
    client = _make_client(
        market_fns={"get_history_bar": lambda *_: BadJsonResponse()},
    )
    with pytest.raises(WebullMalformedResponseError):
        client.get_historical_bars(
            HistoricalBarsRequest(symbol="AAPL", timespan=BarTimespan.DAY)
        )


def test_historical_bars_timestamp_in_millis() -> None:
    client = _make_client(
        market_fns={
            "get_history_bar": lambda *_: FakeResponse(
                [
                    {
                        "timeStamp": "1710000000000",  # 13-digit => ms
                        "open": "1",
                        "high": "1",
                        "low": "1",
                        "close": "1",
                        "volume": "1",
                    }
                ]
            )
        },
    )
    result = client.get_historical_bars(
        HistoricalBarsRequest(symbol="AAPL", timespan=BarTimespan.DAY)
    )
    assert result.bars[0].timestamp.year == 2024


# --------------------------------------------------------------------------- #
# Order status (read-only lookup)
# --------------------------------------------------------------------------- #

_ORDER_OK = {
    "client_order_id": "cli-123",
    "order_id": "brk-999",
    "symbol": "AAPL",
    "order_status": "PARTIAL FILLED",
    "filled_quantity": "5",
    "quantity": "10",
    "avg_fill_price": "150.25",
}


def test_order_status_happy_path_normalises_status() -> None:
    client = _make_client(
        order_fns={"query_order_detail": lambda *_: FakeResponse(_ORDER_OK)},
    )
    result = client.get_order_status(
        OrderStatusRequest(account_id="ACC1", client_order_id="cli-123")
    )
    assert result.client_order_id == "cli-123"
    assert result.broker_order_id == "brk-999"
    assert result.status is OrderStatus.PARTIAL_FILLED  # "PARTIAL FILLED" -> enum
    assert result.filled_quantity == Decimal("5")
    assert result.total_quantity == Decimal("10")
    assert isinstance(result.avg_fill_price, Decimal)


def test_order_status_unknown_status_maps_to_unknown() -> None:
    client = _make_client(
        order_fns={
            "query_order_detail": lambda *_: FakeResponse(
                {"client_order_id": "cli-123", "order_status": "SOMETHING_NEW"}
            )
        },
    )
    result = client.get_order_status(
        OrderStatusRequest(account_id="ACC1", client_order_id="cli-123")
    )
    assert result.status is OrderStatus.UNKNOWN


def test_order_status_timeout() -> None:
    client = _make_client(
        order_fns={
            "query_order_detail": _raises(
                ClientException(error_code.SDK_HTTP_ERROR, "read timed out")
            )
        },
    )
    with pytest.raises(WebullTimeoutError):
        client.get_order_status(
            OrderStatusRequest(account_id="ACC1", client_order_id="cli-123")
        )


def test_order_status_auth_failure() -> None:
    client = _make_client(
        order_fns={
            "query_order_detail": _raises(
                ClientException(error_code.SDK_INVALID_CREDENTIAL, "bad key")
            )
        },
    )
    with pytest.raises(WebullAuthError):
        client.get_order_status(
            OrderStatusRequest(account_id="ACC1", client_order_id="cli-123")
        )


def test_order_status_malformed() -> None:
    client = _make_client(
        order_fns={"query_order_detail": lambda *_: BadJsonResponse()},
    )
    with pytest.raises(WebullMalformedResponseError):
        client.get_order_status(
            OrderStatusRequest(account_id="ACC1", client_order_id="cli-123")
        )


# --------------------------------------------------------------------------- #
# Exception translation table
# --------------------------------------------------------------------------- #


def test_rate_limit_server_exception_maps_to_rate_limit_error() -> None:
    client = _make_client(
        market_fns={
            "get_history_bar": _raises(
                ServerException("RL", "too many", http_status=429)
            )
        },
    )
    with pytest.raises(WebullRateLimitError):
        client.get_historical_bars(
            HistoricalBarsRequest(symbol="AAPL", timespan=BarTimespan.DAY)
        )


def test_generic_server_error_maps_to_api_error() -> None:
    client = _make_client(
        market_fns={
            "get_history_bar": _raises(
                ServerException("SRV", "boom", http_status=500)
            )
        },
    )
    with pytest.raises(WebullAPIError) as excinfo:
        client.get_historical_bars(
            HistoricalBarsRequest(symbol="AAPL", timespan=BarTimespan.DAY)
        )
    assert excinfo.value.http_status == 500


def test_unexpected_raw_exception_is_wrapped_never_leaks() -> None:
    client = _make_client(
        market_fns={"get_history_bar": _raises(RuntimeError("surprise"))},
    )
    # A bare RuntimeError from the SDK must not escape as a RuntimeError; it is
    # wrapped in the base WebullError so a single `except WebullError` fails closed.
    with pytest.raises(WebullError) as excinfo:
        client.get_historical_bars(
            HistoricalBarsRequest(symbol="AAPL", timespan=BarTimespan.DAY)
        )
    assert not isinstance(excinfo.value, WebullAPIError)
