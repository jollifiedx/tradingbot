"""Typed exceptions for the Webull client wrapper.

Every raw SDK / transport exception is translated into one of these before it
leaves the wrapper. Callers (worker, reconciliation, MCP dev tools) only ever
see these types — they never import ``webull.*`` and never catch a raw
``ClientException`` / ``ServerException`` / ``requests`` error. This is the
choke point that keeps the SDK's surface out of the rest of the codebase.

Design note (fail closed): the worker treats *any* ``WebullError`` as a reason
to distrust broker state. Timeouts, rate limits and malformed responses are all
subclasses so a single ``except WebullError`` halts safely, while callers that
want to react specifically (e.g. back off on a rate limit) can catch the
narrower type.
"""

from __future__ import annotations


class WebullError(Exception):
    """Base class for every error surfaced by the wrapper.

    ``message`` is safe to log. It is built by the wrapper and never contains
    App Key / App Secret / signature material — raw SDK exception payloads
    (which can embed signed request headers) are deliberately NOT interpolated
    verbatim into these messages.
    """

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class WebullAuthError(WebullError):
    """Authentication / authorization failure (bad key, expired token, 401/403).

    Fail closed: never retry blindly — a persistent auth failure means the
    worker must halt and alert, not loop.
    """


class WebullTimeoutError(WebullError):
    """The request exceeded the configured connect/read timeout.

    A timeout is *ambiguous* for mutating calls (never used here — this wrapper
    is read-only). For reads it simply means "no fresh truth from the broker",
    which the caller must treat as stale-data / fail-closed.
    """


class WebullRateLimitError(WebullError):
    """The broker rejected the call for exceeding a rate limit (HTTP 429).

    Documented Webull limits: ~600 requests/min for trading endpoints and
    ~15 requests/sec for order endpoints. Callers should back off; the wrapper
    ships a coarse client-side limiter to make hitting this rare.
    """


class WebullMalformedResponseError(WebullError):
    """The response was not shaped the way the wrapper expects.

    Covers non-JSON bodies, missing required fields, and values that fail
    Pydantic validation. Treated as fail-closed: an unparseable broker reply is
    not trustworthy state.
    """


class WebullAPIError(WebullError):
    """A server-side error that is not auth / rate-limit specific.

    Carries the broker's HTTP status and error code when available so the
    caller can log/triage without touching the raw SDK exception.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message, code=code)
        self.http_status = http_status
