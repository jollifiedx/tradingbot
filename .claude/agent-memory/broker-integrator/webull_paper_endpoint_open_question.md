---
name: webull-paper-endpoint-open-question
description: Open question — the SDK has no paper/live switch and ships only LIVE endpoint hosts; the exact Webull paper-trading host + how credentials select paper vs live is unconfirmed and must be resolved before paper data flows over the network.
metadata:
  type: project
---

The Webull SDK (2.0.14) exposes **no paper/live toggle**; its bundled
`endpoints.json` maps regions only to LIVE hosts (US → api.webull.com). See
[[webull-sdk-quirks]].

**Why:** the client wrapper must route off `settings.webull_env` (paper|live)
and never hardcode either, but the concrete mechanism that makes a request hit
the paper environment is not yet confirmed. Two candidates: (a) a distinct paper
host to be set via `endpoint_overrides` / `ApiClient.add_endpoint`, or (b) same
host + paper-scoped App Key/Secret + a paper `account_id` returned by the
account-list endpoint. CLAUDE.md implies separate keys per env, which points at
(b) or a combination.

**How to apply:** the wrapper is built and unit-tested with fully mocked SDK, so
this does NOT block the wrapper. But before any wrapper method is pointed at a
real paper account over the network, confirm the paper routing against
developer.webull.com/apis/docs and set the seam accordingly. Do NOT guess a
paper hostname in code.

**Status (2026-07-20):** env now REALLY gates host routing (was a label). The
owner supplies the paper host via `WEBULL_PAPER_API_ENDPOINT` (Settings field
`webull_paper_api_endpoint`); the wrapper's `_resolve_endpoint_overrides()` maps
it onto the SDK `DEFAULT` (trade/account) api-type when `webull_env == paper`,
raises `WebullConfigError` if it is unset/blank (never falls back to live), and
raises `NotImplementedError` for `webull_env == live` (later owner-gated
milestone). This commits to candidate (a) — a distinct paper *trade* host — for
routing; whether paper also needs paper-scoped credentials/account_id
(candidate b) is still unconfirmed. Market-data/quotes deliberately stay on the
live host in paper (real quotes for paper trading); a separate paper quotes host,
if it exists, goes through the explicit `endpoint_overrides` seam.

**FIRST REAL SANDBOX CALL (2026-07-20, Task 3):** `.env` populated for paper —
paper App Key/Secret, `WEBULL_ENV=paper`, `WEBULL_PAPER_API_ENDPOINT=
api.sandbox.webull.com` (Webull's documented sandbox host). Built a real
`WebullClient` from Settings and made read-only account calls. Result:

- **Host is reachable and legitimate.** `api.sandbox.webull.com` resolves (4x
  IPv4), TCP:443 connects, TLS handshake OK with a valid `*.sandbox.webull.com`
  cert. So candidate-(a)'s host value is not a typo/dead host.
- **But the trade/account API path returns HTTP 404.** The account-list call
  (`account.get_app_subscriptions`, the SDK's read-only account discovery) got a
  clean `ServerException`: `http_status=404`, `error_code='SDK.UnknownServerError'`,
  empty `error_msg`, real `request_id` present. Repeatable (one earlier attempt
  flaked as a transient `ClientException(SDK.HttpError)` transport blip, then
  every subsequent call was a stable 404).
- **404, NOT 401/403.** Credentials were never even evaluated for this path —
  this is a *routing/endpoint-not-found* signal, not an auth rejection. So we
  STILL cannot say whether the paper creds are valid; we can say the SDK's
  trade/account request path does not exist at `api.sandbox.webull.com`.
- **Parsers were never reached** — no real balance/position JSON was obtained, so
  the "do live JSON shapes match `_parse_balance`/`_parse_position`" question is
  still OPEN and untested against real data.
- **Bonus verify:** the wrapper's `_translate_error` correctly maps this real SDK
  `ServerException(404)` → `WebullAPIError(message='Webull server error',
  code='SDK.UnknownServerError', http_status=404)`. Real-exception translation
  works, not just mocked.

**Resolution of candidate (a) vs (b):** partial. A distinct paper host set via
the override seam (candidate a) is NOT sufficient by itself with this host value —
the account/trade service 404s there. Either (i) sandbox trade/account lives at a
different host or path prefix than `api.sandbox.webull.com` + the SDK's built
path, or (ii) sandbox only serves some services (e.g. quotes) and paper trade
uses the live host with paper-scoped creds/account_id (candidate b), or (iii)
sandbox trade access needs enrollment/allowlisting first. NEXT: check
developer.webull.com/apis/docs for the exact sandbox base URL + path for the
trade/account (account-list, balance) APIs and whether sandbox trade requires
sign-up; do NOT guess another hostname in code. Until resolved, no paper account
snapshot can be read over the network.
