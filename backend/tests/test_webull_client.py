"""Unit tests for the read-only Webull client wrapper.

The SDK is fully mocked: the underlying ``TradeClient`` / ``DataClient`` are
replaced with fakes, so no test touches the network or needs real credentials.
``Settings`` is always constructed with explicit dummy values, so the suite is
green with an empty environment and never reads real keys.

Coverage per public method: happy path + timeout + malformed response + auth
failure, plus exception-translation and safety-posture checks.
"""

from __future__ import annotations

import logging
import sys
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest

# Importing the SDK's exception types here (test-only) to simulate raw SDK
# failures. Production code confines SDK imports to app/core/webull/client.py.
from webull.core.common.api_type import DEFAULT as _SDK_TRADE_API_TYPE
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
    WebullConfigError,
    WebullError,
    WebullMalformedResponseError,
    WebullRateLimitError,
    WebullTimeoutError,
)
from app.core.webull import client as client_mod

# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #


def _dummy_settings(
    env: WebullEnv = WebullEnv.PAPER,
    *,
    paper_endpoint: str | None = None,
) -> Settings:
    """Settings with all-dummy values; explicit kwargs override any .env file."""
    return Settings(
        webull_app_key="dummy-key",
        webull_app_secret="dummy-secret",
        webull_env=env,
        webull_paper_api_endpoint=paper_endpoint,
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
        # SDK 2.0.14: the working (``/openapi/...``) account paths live on the
        # ``account_v2`` sub-client, not the v1 ``account`` object.
        account_v2=_Namespace(**(account_fns or {})),
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


class FakeApiClient:
    """Records how the wrapper constructs the SDK ``ApiClient``.

    Captures constructor kwargs (so tests can assert ``auto_retry`` etc.) and
    every ``add_endpoint`` call (so tests can assert host routing). Allows
    arbitrary attribute assignment so the wrapper's ``_stream_logger_set`` seam
    can be observed.
    """

    instances: list[FakeApiClient] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.endpoints: list[tuple[str, str, str]] = []
        # Set by the wrapper's _build_api_client seam (documented SDK quirk);
        # declared here so mypy knows the attribute exists.
        self._stream_logger_set: bool = False
        FakeApiClient.instances.append(self)

    def add_endpoint(self, region_id: str, host: str, api_type: str) -> None:
        self.endpoints.append((region_id, host, api_type))


def _patch_sdk_build(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch the SDK symbols in client.py so ``_build_api_client`` never hits the
    network: ApiClient becomes a recorder, Trade/DataClient become cheap fakes."""
    FakeApiClient.instances.clear()
    monkeypatch.setattr(client_mod, "ApiClient", FakeApiClient)
    monkeypatch.setattr(client_mod, "TradeClient", lambda _api: _Namespace())
    monkeypatch.setattr(client_mod, "DataClient", lambda _api: _Namespace())


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


# Real top-level field names observed from the live Webull sandbox
# (/openapi/assets/balance, SDK 2.0.14). Locks the parser to the proven shape so
# a rename back to the old guessed names regresses loudly. buying_power /
# settled_funds are intentionally absent (they are not top-level in the sandbox
# response), so they parse as None.
_BALANCE_REAL_SANDBOX = {
    "total_asset_currency": "USD",
    "total_net_liquidation_value": "10000.50",
    "total_cash_balance": "2500.25",
    "total_market_value": "7500.25",
    "total_day_profit_loss": "12.34",
    "total_unrealized_profit_loss": "98.99",
    "account_currency_assets": [],
}


def test_account_snapshot_parses_real_sandbox_balance_field_names() -> None:
    client = _make_client(
        account_fns={
            "get_account_balance": lambda *_: FakeResponse(_BALANCE_REAL_SANDBOX),
            "get_account_position": lambda *_: FakeResponse([]),
        },
    )
    snap = client.get_account_snapshot(AccountSnapshotRequest(account_id="ACC1"))
    assert snap.balance.currency == "USD"
    assert snap.balance.net_liquidation == Decimal("10000.50")
    assert snap.balance.total_cash == Decimal("2500.25")
    # No top-level account id in the sandbox balance -> falls back to request id.
    assert snap.balance.account_id == "ACC1"
    # Not top-level in the sandbox response -> None (real names unconfirmed).
    assert snap.balance.buying_power is None
    assert snap.balance.settled_funds is None


def test_account_snapshot_positions_single_call_keyed_on_account_id() -> None:
    """SDK v2 ``account_v2.get_account_position(account_id)`` returns every
    position in one un-paged response and is passed only the account id."""
    call_log: list[tuple[Any, ...]] = []

    def _positions(*args: Any) -> Any:
        call_log.append(args)
        return FakeResponse(
            [
                {"instrument_id": "1", "symbol": "AAA", "quantity": "1"},
                {"instrument_id": "2", "symbol": "BBB", "quantity": "2"},
                {"instrument_id": "3", "symbol": "CCC", "quantity": "3"},
            ]
        )

    client = _make_client(
        account_fns={
            "get_account_balance": lambda *_: FakeResponse(_BALANCE_OK),
            "get_account_position": _positions,
        },
    )
    snap = client.get_account_snapshot(AccountSnapshotRequest(account_id="ACC1"))

    assert [p.symbol for p in snap.positions] == ["AAA", "BBB", "CCC"]
    # Exactly one positions call, receiving only the account id (no paging args).
    assert call_log == [("ACC1",)]


# --------------------------------------------------------------------------- #
# Account list (id discovery — /openapi/account/list)
# --------------------------------------------------------------------------- #

_ACCOUNTS_OK = [
    {
        "account_id": "ACC1",
        "account_number": "5XX-12345678",
        "account_type": "CASH",
        "currency": "USD",
        "status": "ACTIVE",
    }
]


def test_list_accounts_happy_path() -> None:
    limiter = CountingLimiter()
    client = _make_client(
        account_fns={"get_account_list": lambda *_: FakeResponse(_ACCOUNTS_OK)},
        limiter=limiter,
    )
    accounts = client.list_accounts()

    assert len(accounts) == 1
    assert accounts[0].account_id == "ACC1"
    assert accounts[0].account_type == "CASH"
    assert limiter.calls == 1


def test_list_accounts_accepts_wrapped_envelope() -> None:
    client = _make_client(
        account_fns={
            "get_account_list": lambda *_: FakeResponse({"accounts": _ACCOUNTS_OK})
        },
    )
    accounts = client.list_accounts()
    assert [a.account_id for a in accounts] == ["ACC1"]


def test_list_accounts_auth_failure() -> None:
    client = _make_client(
        account_fns={
            "get_account_list": _raises(
                ServerException("AUTH", "forbidden", http_status=403)
            )
        },
    )
    with pytest.raises(WebullAuthError):
        client.list_accounts()


def test_list_accounts_malformed() -> None:
    client = _make_client(
        account_fns={"get_account_list": lambda *_: BadJsonResponse()},
    )
    with pytest.raises(WebullMalformedResponseError):
        client.list_accounts()


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


# --------------------------------------------------------------------------- #
# Safety regression guards (architect DRIFT): these lock down two behaviours
# that are otherwise invisible (they only matter when the real SDK is built),
# so they cannot silently regress.
# --------------------------------------------------------------------------- #


def test_sdk_client_built_with_auto_retry_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """Idempotency invariant: the SDK must never blind-retry for us. If anyone
    flips ``auto_retry`` on (or drops the kwarg), this fails."""
    _patch_sdk_build(monkeypatch)
    client = WebullClient(_dummy_settings(paper_endpoint="https://paper.example.test"))
    client._ensure_clients()  # noqa: SLF001 - triggers the lazy SDK build under test

    assert len(FakeApiClient.instances) == 1
    built = FakeApiClient.instances[0]
    assert "auto_retry" in built.kwargs, "auto_retry must be passed explicitly"
    assert built.kwargs["auto_retry"] is False


def test_sdk_logging_silencing_mechanism_in_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The credential-leak silencing must be active after a client is built:
    the ``webull`` logger carries only a NullHandler, does not propagate, and the
    per-client ``_stream_logger_set`` flag is set so the SDK skips its own
    stdout/file loggers. Fails if any of those mechanisms is removed."""
    sdk_logger = logging.getLogger("webull")
    saved_handlers = sdk_logger.handlers
    saved_propagate = sdk_logger.propagate
    try:
        # Put the logger into a deliberately LEAKY state first, so this proves
        # the build *actively* silences it rather than finding it already quiet.
        sdk_logger.handlers = [logging.StreamHandler(sys.stdout)]
        sdk_logger.propagate = True

        _patch_sdk_build(monkeypatch)
        client = WebullClient(
            _dummy_settings(paper_endpoint="https://paper.example.test")
        )
        client._ensure_clients()  # noqa: SLF001 - triggers the lazy SDK build

        assert sdk_logger.propagate is False
        assert len(sdk_logger.handlers) == 1
        assert isinstance(sdk_logger.handlers[0], logging.NullHandler)
        built = FakeApiClient.instances[0]
        assert built._stream_logger_set is True  # noqa: SLF001 - documented seam
    finally:
        sdk_logger.handlers = saved_handlers
        sdk_logger.propagate = saved_propagate


def test_sdk_log_record_with_secret_does_not_leak(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Any,
) -> None:
    """A simulated SDK log record carrying a fake secret must reach neither
    stdout/stderr nor a log file after the client is built."""
    fake_secret = "SECRET-app-secret-DO-NOT-LEAK-9f2b"  # noqa: S105 - test literal
    sdk_logger = logging.getLogger("webull")
    saved_handlers = sdk_logger.handlers
    saved_propagate = sdk_logger.propagate
    try:
        # Leaky starting state (would print the secret if silencing were removed).
        sdk_logger.handlers = [logging.StreamHandler(sys.stdout)]
        sdk_logger.propagate = True
        monkeypatch.chdir(tmp_path)  # any rogue file logger would land here

        _patch_sdk_build(monkeypatch)
        client = WebullClient(
            _dummy_settings(paper_endpoint="https://paper.example.test")
        )
        client._ensure_clients()  # noqa: SLF001 - triggers the lazy SDK build

        # Emit exactly the kind of record the SDK logs at ERROR (request vars can
        # include signed auth headers) on a child logger of "webull".
        logging.getLogger("webull.trade.trade_client").error(
            "request=%s", {"x-app-secret": fake_secret}
        )
        for handler in logging.getLogger("webull").handlers:
            handler.flush()

        captured = capsys.readouterr()
        assert fake_secret not in captured.out
        assert fake_secret not in captured.err
        # No log file anywhere under the CWD should contain the secret either.
        for path in tmp_path.rglob("*"):
            if path.is_file():
                assert fake_secret not in path.read_text(errors="ignore")
    finally:
        sdk_logger.handlers = saved_handlers
        sdk_logger.propagate = saved_propagate


# --------------------------------------------------------------------------- #
# Environment-gated host routing (architect NOTE): env must REALLY gate the
# host the SDK talks to — not just a descriptive label — and fail closed.
# --------------------------------------------------------------------------- #


def test_paper_build_routes_to_paper_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """paper + endpoint set → the trade/account api host is overridden to the
    paper endpoint (the SDK otherwise resolves only LIVE hosts)."""
    _patch_sdk_build(monkeypatch)
    paper_host = "https://paper-api.example.test"
    client = WebullClient(_dummy_settings(paper_endpoint=paper_host))
    client._ensure_clients()  # noqa: SLF001 - triggers the lazy SDK build

    built = FakeApiClient.instances[0]
    assert (client._region_id, paper_host, _SDK_TRADE_API_TYPE) in built.endpoints  # noqa: SLF001


def test_paper_build_missing_endpoint_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """paper + blank endpoint → typed config error; the wrapper must NEVER fall
    back to the SDK's default (live) host."""
    _patch_sdk_build(monkeypatch)
    client = WebullClient(_dummy_settings(paper_endpoint=None))
    with pytest.raises(WebullConfigError):
        client._ensure_clients()  # noqa: SLF001 - triggers the lazy SDK build
    # And a whitespace-only value is treated the same as blank.
    client_ws = WebullClient(_dummy_settings(paper_endpoint="   "))
    with pytest.raises(WebullConfigError):
        client_ws._ensure_clients()  # noqa: SLF001
    # No live host was ever registered.
    assert FakeApiClient.instances == []


def test_live_build_raises_not_implemented(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """live → NotImplementedError at build time, even if a paper endpoint happens
    to be set. Live is a later, owner-gated milestone."""
    _patch_sdk_build(monkeypatch)
    client = WebullClient(
        _dummy_settings(env=WebullEnv.LIVE, paper_endpoint="https://paper.example.test")
    )
    with pytest.raises(NotImplementedError):
        client._ensure_clients()  # noqa: SLF001 - triggers the lazy SDK build
    assert FakeApiClient.instances == []


def test_explicit_endpoint_override_wins_over_paper_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit per-api-type override (the seam for a paper quotes host) takes
    precedence over the env-derived paper default for the same api type."""
    _patch_sdk_build(monkeypatch)
    override_host = "https://explicit.example.test"
    client = WebullClient(
        _dummy_settings(paper_endpoint="https://paper-api.example.test"),
        endpoint_overrides={_SDK_TRADE_API_TYPE: override_host},
    )
    client._ensure_clients()  # noqa: SLF001 - triggers the lazy SDK build

    built = FakeApiClient.instances[0]
    hosts_for_trade = [
        host for (_region, host, api_type) in built.endpoints
        if api_type == _SDK_TRADE_API_TYPE
    ]
    assert hosts_for_trade == [override_host]
