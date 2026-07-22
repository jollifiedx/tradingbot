"""Market hours -- decided by ``exchange_calendars`` and by nothing else.

CLAUDE.md: "``exchange_calendars`` (sole authority on market hours; never
hand-roll)". So this module contains **no** clock times, no 09:30/16:00, no
holiday list, no half-day list, and no timezone arithmetic of its own. It asks
the calendar and reports the answer. Every hardcoded market time anyone has ever
written has eventually been wrong -- half days, DST shifts, a new federal
holiday, an unscheduled close.

Fail closed (invariant #3): if the question cannot be answered -- a naive
datetime, a date outside the calendar's bounds, a calendar error -- the answer
is "the market is closed" / "no close known". The worker then does less, never
more. "I don't know" is never permission.

Everything in and out is tz-aware UTC (project convention). A *naive* datetime is
rejected rather than assumed-UTC: ``exchange_calendars`` silently localises a
naive timestamp to UTC, which would turn a local-time bug into a confident wrong
answer about whether the market is open.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import exchange_calendars as xcals
import structlog

log = structlog.get_logger()

DEFAULT_CALENDAR = "XNYS"
"""NYSE. The bot trades US equities on Webull; XNYS is the session authority."""


class MarketClock:
    """Thin, fail-closed wrapper over one ``exchange_calendars`` calendar.

    Constructed once at worker startup (building a calendar is expensive: it
    materialises every session in its bounded range). Stateless afterwards, so a
    single instance is safe to share across jobs.
    """

    __slots__ = ("_calendar", "_name")

    def __init__(self, calendar_name: str = DEFAULT_CALENDAR) -> None:
        self._name = calendar_name
        # No stubs ship with exchange_calendars; the boundary is typed here and
        # every value that leaves this module is isinstance-checked below.
        self._calendar: Any = xcals.get_calendar(calendar_name)

    @property
    def name(self) -> str:
        return self._name

    def is_open(self, when: datetime) -> bool:
        """True only if the exchange is open during the minute ``when`` falls in.

        Weekends, holidays, half-day early closes and DST are all the calendar's
        business, not ours. Any doubt returns ``False``.
        """
        if not _is_aware(when):
            log.warning(
                "market_hours.naive_datetime_rejected",
                calendar=self._name,
                detail="a naive datetime cannot be resolved to a market minute",
            )
            return False
        try:
            return bool(self._calendar.is_open_on_minute(when))
        except Exception as exc:
            # Out-of-bounds minute, or any calendar failure: we do not know, so
            # the market is treated as closed.
            log.warning(
                "market_hours.unanswerable",
                calendar=self._name,
                question="is_open",
                error_type=type(exc).__name__,
            )
            return False

    def previous_close(self, when: datetime) -> datetime | None:
        """The most recent session close strictly before ``when`` (UTC), or None.

        Used to answer "has the market closed since the last time we looked?"
        without knowing what time the close is -- on a half day it is 13:00 ET,
        in winter the same 16:00 ET close is 21:00 UTC, and neither fact is
        written down here.
        """
        if not _is_aware(when):
            log.warning(
                "market_hours.naive_datetime_rejected",
                calendar=self._name,
                detail="a naive datetime cannot be resolved to a session close",
            )
            return None
        try:
            close = self._calendar.previous_close(when)
        except Exception as exc:
            log.warning(
                "market_hours.unanswerable",
                calendar=self._name,
                question="previous_close",
                error_type=type(exc).__name__,
            )
            return None
        return _as_aware_datetime(close)


def _is_aware(value: datetime) -> bool:
    """True only for a genuinely timezone-aware datetime."""
    return value.tzinfo is not None and value.tzinfo.utcoffset(value) is not None


def _as_aware_datetime(value: Any) -> datetime | None:
    """Convert a calendar timestamp to an aware ``datetime``, or None if it isn't one.

    ``exchange_calendars`` returns ``pandas.Timestamp``. Rather than depend on
    pandas' typing here, the value is converted duck-style and then proven to be
    an aware ``datetime`` before it escapes this module -- an unusable timestamp
    becomes ``None`` (fail closed) instead of a subtly wrong datetime.
    """
    converter = getattr(value, "to_pydatetime", None)
    converted = converter() if callable(converter) else value
    if not isinstance(converted, datetime) or not _is_aware(converted):
        return None
    return converted
