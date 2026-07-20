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
- **Account-id discovery methods (read-only).** The wrapper takes `account_id`
  as input; to *find* it, v1 `trade.account` exposes `get_app_subscriptions()`
  (docstring: "query the account list and return account information") and the v2
  object `trade.account_v2` exposes `get_account_list()`. v1 `Account` methods:
  `get_account_balance(account_id, total_asset_currency)`,
  `get_account_position(account_id, page_size=10, last_instrument_id=None)`,
  `get_account_profile`, `get_app_subscriptions`. (No `webull.trade.api` /
  `webull.trade.api_request` modules exist — real path is
  `webull.trade.trade.account_info.Account`.)
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
