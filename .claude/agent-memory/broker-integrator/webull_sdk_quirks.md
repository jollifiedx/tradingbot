---
name: webull-sdk-quirks
description: Non-obvious behaviors of webull-openapi-python-sdk (v2.0.14) discovered while building the client wrapper — import name, hidden network I/O, credential-leaking logging, paper/live gap, timeout masking.
metadata:
  type: reference
---

Quirks of `webull-openapi-python-sdk` (installed 2.0.14, Python 3.12). All SDK
access is confined to `backend/app/core/webull/client.py`. Verify against the
current SDK version before relying on any of these — they were true at 2.0.14.

- **Import name is `webull`, not `webullsdkcore`.** Top-level package is `webull`
  (`webull.core`, `webull.trade`, `webull.data`). `TradeClient` lives at
  `webull.trade.trade_client`, `DataClient` at `webull.data.data_client`,
  `ApiClient` at `webull.core.client`.
- **No `py.typed`** → mypy strict needs a scoped override
  (`[[tool.mypy.overrides]] module=["webull.*"] ignore_missing_imports=true`),
  not a global relax. SDK objects arrive as `Any` and are parsed into Pydantic
  at the boundary.
- **Constructing `TradeClient`/`DataClient` does network I/O.** Their `__init__`
  calls `ClientInitializer` → `config_operation.get_config()` (a live HTTP probe
  for `token_check_enabled`). So construction must be lazy — never build them at
  import/DI time. `ApiClient.__init__` itself is cheap.
- **Credential-leaking logging (fail-open by default).** On first client build
  the SDK installs a stdout logger AND a rotating file logger
  (`webull_trade_sdk.log` / `webull_data_sdk.log` in CWD). Worse, `get_response`
  / `_handle_single_request` log full request `vars()` — which include signed
  auth headers — at ERROR. Suppress by (a) setting
  `api_client._stream_logger_set = True` before building Trade/Data clients so
  it skips its own logger setup, and (b) attaching a `NullHandler` +
  `propagate=False` on the `webull` logger. The wrapper does both.
- **`get_response()` returns a `requests.Response`.** Body via `.json()`. Every
  API method (`account.get_account_balance`, `market_data.get_history_bar`,
  `order.query_order_detail`, etc.) returns this same object.
- **Timeouts are masked as `ClientException(SDK.HttpError)`.** `requests` errors
  are `IOError` subclasses, caught in `_handle_single_request` and rewrapped as
  `ClientException(error_code.SDK_HTTP_ERROR, "<...timed out...>")`. You cannot
  tell a timeout from a connection error by exception type — must sniff the
  message. Server-side errors come as `ServerException` with `http_status` /
  `error_code`.
- **No paper/live switch in the SDK.** `endpoints.json` only maps regions to
  LIVE hosts (US = api.webull.com / data-api.webull.com / events-api.webull.com).
  Paper vs live is credential- and (likely) host-scoped, not an SDK flag. The
  wrapper drives it off `settings.webull_env` and exposes an `endpoint_overrides`
  seam (`ApiClient.add_endpoint(region, host, api_type)`). See
  [[webull-paper-endpoint-open-question]] — exact paper host is unconfirmed.
- **Three endpoint `api_type` constants** in `webull.core.common.api_type`:
  `DEFAULT = "api"` (trade + account host), `QUOTES = "quotes-api"` (market
  data), `EVENTS = "events-api"` (streaming). `add_endpoint(region, host,
  api_type)` overrides ONE api-type's host at a time; `_resolve_endpoint(request)`
  only takes `region_id`, so per-api-type routing is the override key. The
  wrapper maps the paper endpoint onto `DEFAULT` only (trade/account is what
  distinguishes paper money from live; quotes stay on the live feed).
- **Enums serialize to their member NAME** (`EasyEnum.__str__` returns `.name`).
  `Timespan`: M1,M5,M15,M30,M60,M120,M240,D,W,M,Y. `Category`: US_STOCK,
  US_ETF, US_OPTION, US_CRYPTO, ... `OrderStatus`: SUBMITTED, CANCELLED, FAILED,
  FILLED, PARTIAL_FILLED (label is "PARTIAL FILLED" with a space — normalise
  space→underscore when mapping). Query params take the name string.
- **USE `account_v2`, NOT `account`, for account reads.** CONFIRMED against the
  live sandbox (2026-07-20): the v1 `trade.account.*` methods build OLD request
  paths (`/account/balance`, `/account/positions`, `/app/subscriptions/list`)
  that **404** at `api.sandbox.webull.com`. The v2 sub-client `trade.account_v2`
  builds the documented `/openapi/...` paths that return **200**:
  - `account_v2.get_account_list()` → `GET /openapi/account/list` (no args) —
    the "Verify Your Setup" account-discovery call. Returns the account list.
  - `account_v2.get_account_balance(account_id)` → `GET /openapi/assets/balance`
    — **account_id only, NO currency arg** (unlike v1). Not paged.
  - `account_v2.get_account_position(account_id)` → `GET /openapi/assets/positions`
    — **account_id only, NO paging args** (unlike v1's page_size/last_instrument_id);
    returns ALL positions in one un-paged response. (`get_account_position_details`
    with paging exists but is JP-only.)
  The wrapper now calls account_v2 exclusively and exposes `list_accounts()`.
  v1 `trade.account` (`get_account_balance(account_id, total_asset_currency)`,
  `get_account_position(account_id, page_size, last_instrument_id)`,
  `get_app_subscriptions`, `get_account_profile`) is a dead/legacy path — do not
  use. Real module: `webull.trade.trade.v2.account_info_v2.AccountV2`.
- **Order reads have the same v1/v2 split (UNPATCHED, flagged).** `trade.order`
  (v1) `query_order_detail` builds `/trade/order/detail`; the working path is
  `trade.order_v2.get_order_detail(account_id, client_order_id)` →
  `/openapi/trade/order/detail`. The wrapper's `get_order_status` still points at
  v1 `trade.order.query_order_detail` and will very likely 404 the same way —
  fix it (switch to `order_v2`) when order-status work is picked up. Left as-is
  here because order-status is a later milestone / execution-guardian's surface.
- **Real sandbox balance field names (CONFIRMED 2026-07-20).**
  `/openapi/assets/balance` top-level keys: `total_asset_currency`,
  `total_net_liquidation_value`, `total_cash_balance`, `total_market_value`,
  `total_day_profit_loss`, `total_unrealized_profit_loss`,
  `account_currency_assets` (a list — per-currency breakdown). There is **no
  top-level `account_id`, `buying_power`, or `settled_funds`.** The old parser
  guessed `net_liquidation_value` / `total_cash_value` / `currency` — all wrong;
  corrected to the real names. `buying_power`/`settled_funds` real names are
  likely nested inside `account_currency_assets` but were NOT probed (call-budget
  cap) — currently parse to None; confirm before cap logic relies on them.
- **`get_account_list` returns `account_type` per account** (values seen:
  `CASH`, `MARGIN`); the sandbox app key saw 5 accounts. Per-account `currency`
  and `status` came back null in the list response.
- **`ServerException` fields are response-side and safe to log.** It carries
  `error_code`, `error_msg`, `http_status`, `request_id` (all from the server's
  reply, no request headers) — unlike the `ClientException`/request-`vars`
  logging leak. A sandbox path-not-found comes back as `http_status=404`,
  `error_code='SDK.UnknownServerError'`, empty `error_msg`, with a real
  `request_id`. See [[webull-paper-endpoint-open-question]] for the sandbox 404.
- **Timeouts are ApiClient-level.** Constructor args `connect_timeout` +
  `timeout` (read) apply to all calls; per-request `set_read_timeout` /
  `set_connect_timeout` also exist but the SDK builds request objects internally
  so the client-level values are what the wrapper uses. `auto_retry=False` keeps
  the SDK from blind-retrying (order-idempotency invariant).
