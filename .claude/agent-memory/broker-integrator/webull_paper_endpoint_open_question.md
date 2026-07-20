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
paper hostname in code. Currently region defaults to "us" and
`endpoint_overrides` is empty (i.e. resolves to LIVE hosts) — that is a
deliberate fail-visible default, not a decision that paper == live.
