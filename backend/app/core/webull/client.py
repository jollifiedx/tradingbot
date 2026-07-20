"""Typed, read-only Webull client wrapper.

This is the **only** module in the codebase that imports ``webull.*``. Every
other layer (worker, reconciliation, dev MCP tools) talks to Webull through
:class:`WebullClient` and receives validated Pydantic models — never a raw SDK
object, never a raw SDK/transport exception.

Scope (read-only, deliberately): account snapshot, historical OHLCV bars, and
order *status* lookup. Order placement / modification / cancellation are NOT
implemented here — see the ``ORDER-PATH SEAM`` marker below. That surface
belongs to execution-guardian's audited order path.

Safety posture:
- paper vs live is driven entirely by ``settings.webull_env`` — never hardcoded
  — and that env *gates host routing* (not just a label): a paper client is
  built against ``settings.webull_paper_api_endpoint`` and refuses to fall back
  to the SDK's default (live) host; a live client raises ``NotImplementedError``
  until live is an owner-gated, explicitly-enabled milestone.
- explicit connect + read timeouts on every call (SDK-level, applied globally).
- a coarse client-side rate limiter keeps us under Webull's documented ceilings
  (~600 req/min trading, ~15 req/sec orders).
- the SDK's own logging (which can echo signed request headers) is silenced so
  App Key / App Secret can never leak to stdout or a log file.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

# --- the single SDK import site -------------------------------------------- #
# webull.* is untyped (no py.typed); mypy is scoped to ignore missing imports
# for that package only (see pyproject [[tool.mypy.overrides]]).
from webull.core.client import ApiClient
from webull.core.common.api_type import DEFAULT as _SDK_TRADE_API_TYPE
from webull.core.exception import error_code
from webull.core.exception.exceptions import ClientException, ServerException
from webull.data.data_client import DataClient
from webull.trade.trade_client import TradeClient

from app.core.config import Settings, WebullEnv

from .exceptions import (
    WebullAPIError,
    WebullAuthError,
    WebullConfigError,
    WebullError,
    WebullMalformedResponseError,
    WebullRateLimitError,
    WebullTimeoutError,
)
from .models import (
    AccountBalance,
    AccountInfo,
    AccountSnapshot,
    AccountSnapshotRequest,
    HistoricalBars,
    HistoricalBarsRequest,
    OHLCVBar,
    OrderStatus,
    OrderStatusRequest,
    OrderStatusResult,
    Position,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

log = logging.getLogger(__name__)

# US market region for the SDK's endpoint resolver. Region is not the same as
# paper/live: both paper and live US trading resolve through the "us" region;
# the environment distinction is credential- and (optionally) host-scoped —
# see the class docstring and README on WEBULL_ENV.
_US_REGION = "us"

_DEFAULT_CONNECT_TIMEOUT_S = 5.0
_DEFAULT_READ_TIMEOUT_S = 10.0

# Coarse client-side rate limit. Trading endpoints allow ~600/min; we stay well
# under. This is a placeholder ceiling, not a precise reproduction of Webull's
# per-endpoint buckets.
_DEFAULT_RATE_LIMIT_CALLS = 600
_DEFAULT_RATE_LIMIT_PERIOD_S = 60.0

_TIMEOUT_HINTS = ("timed out", "timeout", "time out", "read timed out")


def _silence_sdk_logging() -> None:
    """Stop the SDK from logging to stdout / a rotating file.

    The Webull SDK, if left alone, (a) installs a stdout + ``webull_*_sdk.log``
    file logger on first client construction and (b) logs full request ``vars``
    at ERROR — which include signed auth headers. We attach a NullHandler and
    kill propagation on the ``webull`` logger so none of that reaches the root
    logger, and we set the per-client ``_stream_logger_set`` flag (in
    :func:`_build_api_client`) so the SDK skips its own logger setup entirely.
    """
    sdk_logger = logging.getLogger("webull")
    sdk_logger.handlers = [logging.NullHandler()]
    sdk_logger.propagate = False


class _RateLimiter:
    """Minimal thread-safe sliding-window limiter (placeholder).

    Blocks the caller just long enough to keep the number of calls in any
    ``period`` window at or below ``max_calls``. Good enough to avoid tripping
    Webull's limits in the single-user bot; not a distributed limiter.
    """

    def __init__(self, max_calls: int, period: float) -> None:
        self._max_calls = max_calls
        self._period = period
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            while self._calls and now - self._calls[0] >= self._period:
                self._calls.popleft()
            if len(self._calls) >= self._max_calls:
                sleep_for = self._period - (now - self._calls[0])
                if sleep_for > 0:
                    time.sleep(sleep_for)
                now = time.monotonic()
                while self._calls and now - self._calls[0] >= self._period:
                    self._calls.popleft()
            self._calls.append(time.monotonic())


class WebullClient:
    """Read-only, typed facade over the Webull OpenAPI Python SDK."""

    def __init__(
        self,
        settings: Settings,
        *,
        region_id: str = _US_REGION,
        connect_timeout_s: float = _DEFAULT_CONNECT_TIMEOUT_S,
        read_timeout_s: float = _DEFAULT_READ_TIMEOUT_S,
        endpoint_overrides: Mapping[str, str] | None = None,
        rate_limiter: _RateLimiter | None = None,
    ) -> None:
        """Build a client from validated :class:`Settings`.

        ``settings.webull_env`` (paper|live) is captured verbatim and exposed via
        :attr:`env` / :attr:`is_live`. It also *gates host routing* when the SDK
        client is lazily built (see :meth:`_resolve_endpoint_overrides`): paper
        routes to ``settings.webull_paper_api_endpoint`` or fails closed; live
        raises ``NotImplementedError``. ``endpoint_overrides`` maps SDK api-type
        → host and is the seam for pointing a specific api type (e.g. a paper
        *quotes* host) at a distinct host without touching code; entries here win
        over the env-derived default.
        """
        self._settings = settings
        self._env: WebullEnv = settings.webull_env
        self._region_id = region_id
        self._connect_timeout_s = connect_timeout_s
        self._read_timeout_s = read_timeout_s
        self._endpoint_overrides = dict(endpoint_overrides or {})
        self._rate_limiter = rate_limiter or _RateLimiter(
            _DEFAULT_RATE_LIMIT_CALLS, _DEFAULT_RATE_LIMIT_PERIOD_S
        )
        # Lazily built: constructing a TradeClient/DataClient triggers a network
        # call (token-config probe), so we defer it until the first real request.
        self._api_client: Any | None = None
        self._trade_client: Any | None = None
        self._data_client: Any | None = None
        self._build_lock = threading.Lock()

    # -- environment introspection ----------------------------------------- #

    @property
    def env(self) -> WebullEnv:
        return self._env

    @property
    def is_live(self) -> bool:
        return self._env is WebullEnv.LIVE

    # -- lazy SDK construction --------------------------------------------- #

    def _resolve_endpoint_overrides(self) -> dict[str, str]:
        """Return the api-type → host overrides to apply, gated by environment.

        This is where paper/live host routing is *actually enforced* (not merely
        labelled). Fail closed:

        - **live** → :class:`NotImplementedError`. Live is a later, owner-gated
          milestone; the wrapper must never silently build a client that can
          reach live hosts.
        - **paper** → the SDK ships only LIVE hosts, so we MUST override the
          trade/account api host with ``settings.webull_paper_api_endpoint``. If
          that is unset/blank we raise :class:`WebullConfigError` rather than let
          the SDK resolve to its default (live) host. Explicit per-api-type
          ``endpoint_overrides`` passed to the constructor win over this default
          (the seam for a distinct paper *quotes* host, if Webull provides one).

        Market-data (quotes) calls intentionally keep resolving to the live
        quotes host unless explicitly overridden: Webull paper accounts read the
        same live market data; only order/account routing distinguishes paper
        from live money.
        """
        if self._env is WebullEnv.LIVE:
            raise NotImplementedError(
                "Webull live environment is not enabled — live is a later, "
                "owner-gated milestone. Refusing to build a live client."
            )
        if self._env is not WebullEnv.PAPER:
            # Defensive: WebullEnv is a closed enum, but fail closed on anything
            # that is neither paper nor live rather than defaulting to live hosts.
            raise WebullConfigError(f"unrecognised webull_env: {self._env!r}")

        paper_host = (self._settings.webull_paper_api_endpoint or "").strip()
        if not paper_host:
            raise WebullConfigError(
                "WEBULL_ENV=paper but WEBULL_PAPER_API_ENDPOINT is unset/blank; "
                "refusing to fall back to the SDK's default (live) host. Set the "
                "paper endpoint from the Webull developer portal in the "
                "environment before building a paper client."
            )
        overrides: dict[str, str] = {_SDK_TRADE_API_TYPE: paper_host}
        # Explicit constructor overrides take precedence (e.g. a paper quotes host).
        overrides.update(self._endpoint_overrides)
        return overrides

    def _build_api_client(self) -> Any:
        # Resolve host routing FIRST so a misconfigured environment fails closed
        # before we construct anything or touch the network.
        endpoint_overrides = self._resolve_endpoint_overrides()
        _silence_sdk_logging()
        client = ApiClient(
            app_key=self._settings.webull_app_key,
            app_secret=self._settings.webull_app_secret,
            region_id=self._region_id,
            connect_timeout=self._connect_timeout_s,
            timeout=self._read_timeout_s,
            auto_retry=False,  # idempotency: never let the SDK blind-retry for us
        )
        # Pre-set the flag so TradeClient/DataClient skip installing their own
        # stdout + file loggers (see _silence_sdk_logging).
        client._stream_logger_set = True  # noqa: SLF001 - documented SDK seam
        for api_type, host in endpoint_overrides.items():
            client.add_endpoint(self._region_id, host, api_type)
        return client

    def _ensure_clients(self) -> tuple[Any, Any]:
        if self._trade_client is not None and self._data_client is not None:
            return self._trade_client, self._data_client
        with self._build_lock:
            if self._api_client is None:
                self._api_client = self._build_api_client()
            if self._trade_client is None:
                self._trade_client = TradeClient(self._api_client)
            if self._data_client is None:
                self._data_client = DataClient(self._api_client)
        return self._trade_client, self._data_client

    def _trade(self) -> Any:
        trade, _ = self._ensure_clients()
        return trade

    def _data(self) -> Any:
        _, data = self._ensure_clients()
        return data

    # -- public API -------------------------------------------------------- #

    def list_accounts(self) -> tuple[AccountInfo, ...]:
        """Return the brokerage accounts this app key can see (read-only).

        Maps to the SDK's ``account_v2.get_account_list()``
        (``GET /openapi/account/list``) — the documented "Verify Your Setup"
        call. This is how the ``account_id`` the read paths key on is
        discovered; the worker looks it up once at startup rather than
        hardcoding it.
        """
        trade = self._trade()
        body = self._unwrap(self._call(trade.account_v2.get_account_list))
        raw_accounts = self._extract_account_list(body)
        return tuple(self._parse_account_info(raw) for raw in raw_accounts)

    def get_account_snapshot(self, request: AccountSnapshotRequest) -> AccountSnapshot:
        """Return cash/buying-power + all open positions for an account.

        Webull is the source of truth for positions and cash (Invariant 6); the
        worker reconciles DB intent against this snapshot. Uses the SDK v2 assets
        endpoints (``account_v2.get_account_balance`` /
        ``get_account_position``), which key on ``account_id`` alone: balance is
        a single call and positions come back in one un-paged response.
        """
        trade = self._trade()

        balance_body = self._unwrap(
            self._call(trade.account_v2.get_account_balance, request.account_id)
        )
        balance = self._parse_balance(balance_body, request)

        positions_body = self._unwrap(
            self._call(trade.account_v2.get_account_position, request.account_id)
        )
        positions = [
            self._parse_position(raw)
            for raw in self._extract_position_list(positions_body)
        ]
        try:
            return AccountSnapshot(
                balance=balance,
                positions=tuple(positions),
                captured_at=datetime.now(UTC),
            )
        except ValidationError as exc:
            raise WebullMalformedResponseError(
                "account snapshot failed validation"
            ) from exc

    def get_historical_bars(self, request: HistoricalBarsRequest) -> HistoricalBars:
        """Return an oldest→newest OHLCV series for one symbol."""
        data = self._data()
        body = self._unwrap(
            self._call(
                data.market_data.get_history_bar,
                request.symbol,
                request.category.value,
                request.timespan.value,
                str(request.count),
            )
        )
        raw_bars = self._extract_bar_list(body)
        bars = [self._parse_bar(raw) for raw in raw_bars]
        # Webull returns newest-first; normalise to chronological order.
        bars.sort(key=lambda b: b.timestamp)
        try:
            return HistoricalBars(
                symbol=request.symbol,
                timespan=request.timespan,
                bars=tuple(bars),
            )
        except ValidationError as exc:
            raise WebullMalformedResponseError(
                "historical bars failed validation"
            ) from exc

    def get_order_status(self, request: OrderStatusRequest) -> OrderStatusResult:
        """Look up the current status of an order by client order id (read-only).

        This is a *status query*, used by reconciliation and by the idempotency
        rule ("on timeout, query status — never blind-retry a POST"). It cannot
        place, modify or cancel anything.
        """
        trade = self._trade()
        body = self._unwrap(
            self._call(
                trade.order.query_order_detail,
                request.account_id,
                request.client_order_id,
            )
        )
        return self._parse_order_status(body, request)

    # --------------------------------------------------------------------- #
    # ORDER-PATH SEAM — intentionally NOT implemented here.
    #
    # place_order / replace_order / cancel_order are order-*mutating* calls.
    # They live in execution-guardian's audited order path (idempotent client
    # order IDs written to `orders` before submission, live-order approval
    # gating, etc.), never in this read-only wrapper. Do not add them here; a
    # mutating method on this class is an ESCALATION.
    # --------------------------------------------------------------------- #

    # -- call plumbing ----------------------------------------------------- #

    def _call(self, fn: Callable[..., Any], *args: Any) -> Any:
        """Run one SDK call under the rate limiter, translating every failure."""
        self._rate_limiter.acquire()
        try:
            return fn(*args)
        except WebullError:
            raise
        except BaseException as exc:  # noqa: BLE001 - nothing raw may escape
            raise self._translate_error(exc) from exc

    def _translate_error(self, exc: BaseException) -> WebullError:
        """Map any raw SDK / transport exception to a typed wrapper exception.

        Messages are built from the SDK's structured fields (error code / http
        status), never from ``vars(exception)`` — which can carry signed request
        headers.
        """
        if isinstance(exc, ServerException):
            status = getattr(exc, "http_status", None)
            code = getattr(exc, "error_code", None)
            if status in (401, 403) or code == error_code.SDK_INVALID_CREDENTIAL:
                return WebullAuthError("Webull authentication failed", code=code)
            if status == 429:
                return WebullRateLimitError("Webull rate limit exceeded", code=code)
            return WebullAPIError(
                "Webull server error", code=code, http_status=status
            )
        if isinstance(exc, ClientException):
            code = getattr(exc, "error_code", None)
            msg = (getattr(exc, "error_msg", "") or "").lower()
            if code == error_code.SDK_INVALID_CREDENTIAL:
                return WebullAuthError("Webull authentication failed", code=code)
            if code == error_code.SDK_HTTP_ERROR and any(
                hint in msg for hint in _TIMEOUT_HINTS
            ):
                return WebullTimeoutError("Webull request timed out", code=code)
            if code == error_code.SDK_HTTP_ERROR:
                return WebullAPIError("Webull HTTP/transport error", code=code)
            return WebullAPIError("Webull client error", code=code)
        # Defensive: a raw requests timeout or anything else must not escape.
        name = type(exc).__name__.lower()
        if "timeout" in name:
            return WebullTimeoutError("Webull request timed out")
        return WebullError(f"Unexpected Webull SDK failure ({type(exc).__name__})")

    # -- response shaping -------------------------------------------------- #

    @staticmethod
    def _json_body(response: Any) -> Any:
        """Extract a parsed JSON body from the SDK's ``requests.Response``."""
        if response is None:
            raise WebullMalformedResponseError("empty response from Webull")
        json_fn = getattr(response, "json", None)
        if callable(json_fn):
            try:
                return json_fn()
            except (ValueError, TypeError) as exc:
                raise WebullMalformedResponseError(
                    "Webull response body was not valid JSON"
                ) from exc
        # Some call sites may hand us an already-decoded body (tests, futures).
        return response

    @classmethod
    def _unwrap(cls, response: Any) -> Any:
        """Return the payload, peeling a ``{"data": ...}`` envelope if present."""
        body = cls._json_body(response)
        if isinstance(body, dict) and "data" in body:
            return body["data"]
        return body

    @staticmethod
    def _extract_bar_list(body: Any) -> Sequence[Any]:
        if isinstance(body, list):
            # Batch shape: [{"symbol":..., "bars":[...]}] or a flat list of bars.
            if body and isinstance(body[0], dict) and "bars" in body[0]:
                bars = body[0].get("bars")
                return bars if isinstance(bars, list) else []
            return body
        if isinstance(body, dict):
            bars = body.get("bars", [])
            return bars if isinstance(bars, list) else []
        raise WebullMalformedResponseError("unexpected bars payload shape")

    @staticmethod
    def _extract_position_list(body: Any) -> Sequence[Any]:
        if isinstance(body, list):
            return body
        if isinstance(body, dict):
            for key in ("holdings", "positions", "items", "list"):
                value = body.get(key)
                if isinstance(value, list):
                    return value
            return []
        raise WebullMalformedResponseError("unexpected positions payload shape")

    @staticmethod
    def _extract_account_list(body: Any) -> Sequence[Any]:
        if isinstance(body, list):
            return body
        if isinstance(body, dict):
            for key in ("accounts", "account_list", "items", "list"):
                value = body.get(key)
                if isinstance(value, list):
                    return value
            # A single account object (not wrapped in a list) is still valid.
            if "account_id" in body or "accountId" in body:
                return [body]
            return []
        raise WebullMalformedResponseError("unexpected account-list payload shape")

    # -- parsers ----------------------------------------------------------- #

    @staticmethod
    def _pick(source: Mapping[str, Any], *keys: str) -> Any:
        for key in keys:
            value = source.get(key)
            if value is not None:
                return value
        return None

    def _parse_account_info(self, raw: Any) -> AccountInfo:
        if not isinstance(raw, dict):
            raise WebullMalformedResponseError("unexpected account-list entry shape")
        try:
            return AccountInfo(
                account_id=str(self._pick(raw, "account_id", "accountId") or ""),
                account_number=self._opt_str(
                    self._pick(raw, "account_number", "accountNumber")
                ),
                account_type=self._opt_str(
                    self._pick(raw, "account_type", "accountType")
                ),
                currency=self._opt_str(self._pick(raw, "currency")),
                status=self._opt_str(self._pick(raw, "status", "account_status")),
            )
        except ValidationError as exc:
            raise WebullMalformedResponseError(
                "account-list entry failed validation"
            ) from exc

    def _parse_balance(
        self, body: Any, request: AccountSnapshotRequest
    ) -> AccountBalance:
        if isinstance(body, list):
            body = body[0] if body else {}
        if not isinstance(body, dict):
            raise WebullMalformedResponseError("unexpected balance payload shape")
        try:
            # Field names below with a "REAL:" note were confirmed against the
            # live Webull sandbox balance response (SDK 2.0.14,
            # /openapi/assets/balance); the remaining aliases are defensive
            # fallbacks. buying_power / settled_funds are NOT top-level in the
            # sandbox response (they live nested under account_currency_assets);
            # their real names are unconfirmed, so those fields may be None until
            # the nested shape is proven — see broker-integrator memory.
            return AccountBalance(
                # Sandbox balance has no top-level account id; fall back to the
                # requested one (the endpoint is already keyed on it).
                account_id=str(
                    self._pick(body, "account_id", "accountId") or request.account_id
                ),
                # REAL: total_asset_currency
                currency=str(
                    self._pick(body, "total_asset_currency", "currency") or "USD"
                ),
                net_liquidation=self._pick(
                    body,
                    "total_net_liquidation_value",  # REAL
                    "net_liquidation_value",
                    "net_liquidation",
                    "total_asset",
                ),
                total_cash=self._pick(
                    body,
                    "total_cash_balance",  # REAL
                    "total_cash_value",
                    "cash_balance",
                    "total_cash",
                ),
                buying_power=self._pick(body, "buying_power", "day_buying_power"),
                settled_funds=self._pick(
                    body, "settled_funds", "cash_available_for_trading"
                ),
            )
        except ValidationError as exc:
            raise WebullMalformedResponseError(
                "account balance failed validation"
            ) from exc

    def _parse_position(self, raw: Any) -> Position:
        if not isinstance(raw, dict):
            raise WebullMalformedResponseError("unexpected position entry shape")
        try:
            return Position(
                instrument_id=str(self._pick(raw, "instrument_id") or ""),
                symbol=str(self._pick(raw, "symbol", "ticker") or ""),
                quantity=self._pick(raw, "quantity", "position", "qty"),
                cost_price=self._pick(raw, "cost_price", "avg_cost", "unit_cost"),
                market_value=self._pick(raw, "market_value", "mkt_value"),
                unrealized_pnl=self._pick(
                    raw, "unrealized_profit_loss", "unrealized_pnl"
                ),
            )
        except ValidationError as exc:
            raise WebullMalformedResponseError(
                "position entry failed validation"
            ) from exc

    def _parse_bar(self, raw: Any) -> OHLCVBar:
        if not isinstance(raw, dict):
            raise WebullMalformedResponseError("unexpected bar entry shape")
        ts = self._parse_timestamp(self._pick(raw, "timeStamp", "timestamp", "time"))
        try:
            return OHLCVBar(
                timestamp=ts,
                open=self._pick(raw, "open"),
                high=self._pick(raw, "high"),
                low=self._pick(raw, "low"),
                close=self._pick(raw, "close"),
                volume=self._pick(raw, "volume"),
                vwap=self._pick(raw, "vwap"),
            )
        except ValidationError as exc:
            raise WebullMalformedResponseError("bar entry failed validation") from exc

    def _parse_order_status(
        self, body: Any, request: OrderStatusRequest
    ) -> OrderStatusResult:
        if isinstance(body, list):
            body = body[0] if body else {}
        if not isinstance(body, dict):
            raise WebullMalformedResponseError("unexpected order-detail payload shape")
        status = self._normalise_order_status(
            self._pick(body, "order_status", "status")
        )
        try:
            return OrderStatusResult(
                client_order_id=str(
                    self._pick(body, "client_order_id") or request.client_order_id
                ),
                broker_order_id=self._opt_str(
                    self._pick(body, "order_id", "broker_order_id")
                ),
                symbol=self._opt_str(self._pick(body, "symbol", "ticker")),
                status=status,
                filled_quantity=self._pick(body, "filled_quantity", "filled_qty"),
                total_quantity=self._pick(body, "quantity", "total_quantity", "qty"),
                avg_fill_price=self._pick(
                    body, "avg_fill_price", "average_filled_price", "filled_price"
                ),
            )
        except ValidationError as exc:
            raise WebullMalformedResponseError(
                "order status failed validation"
            ) from exc

    # -- value helpers ----------------------------------------------------- #

    @staticmethod
    def _opt_str(value: Any) -> str | None:
        return None if value is None else str(value)

    @staticmethod
    def _normalise_order_status(value: Any) -> OrderStatus:
        if value is None:
            return OrderStatus.UNKNOWN
        key = str(value).strip().upper().replace(" ", "_")
        try:
            return OrderStatus(key)
        except ValueError:
            return OrderStatus.UNKNOWN

    @staticmethod
    def _parse_timestamp(value: Any) -> datetime:
        """Parse a Webull bar timestamp into a UTC-aware datetime.

        Accepts epoch seconds or milliseconds (int/str) and ISO-8601 strings.
        """
        if value is None:
            raise WebullMalformedResponseError("bar is missing a timestamp")
        if isinstance(value, (int, float)) or (
            isinstance(value, str) and value.lstrip("-").isdigit()
        ):
            try:
                epoch = Decimal(str(value))
            except (InvalidOperation, ValueError) as exc:
                raise WebullMalformedResponseError(
                    "bar timestamp was not a valid epoch"
                ) from exc
            # >= 1e12 → milliseconds; otherwise seconds.
            seconds = epoch / 1000 if abs(epoch) >= 1_000_000_000_000 else epoch
            return datetime.fromtimestamp(float(seconds), tz=UTC)
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value)
            except ValueError as exc:
                raise WebullMalformedResponseError(
                    "bar timestamp was not a valid ISO-8601 string"
                ) from exc
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        raise WebullMalformedResponseError("bar timestamp had an unsupported type")
