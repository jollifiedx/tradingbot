"""Tests for the market-hours wrapper.

CLAUDE.md makes `exchange_calendars` the SOLE authority on market hours. These
tests pin that with real, dated cases -- a weekend, a real holiday, a real half
day, and a DST-shifted close -- because the failure mode of a hand-rolled
calendar is a worker that reconciles (and one day trades) while the exchange is
shut, and nothing else in the system would notice.

Dates used (NYSE / XNYS):
- 2026-07-25  Saturday.
- 2026-01-01  New Year's Day, a Thursday -- a weekday the market is shut.
- 2026-11-27  the Friday after Thanksgiving -- a half day, closing 13:00 ET.
- 2026-11-25  the Wednesday before -- an ordinary full session, for contrast.
- 2026-01-02  a January session -- EST, so the 16:00 ET close is 21:00 UTC.
- 2026-07-21  a July session -- EDT, so the same close is 20:00 UTC.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.worker.market_hours import DEFAULT_CALENDAR, MarketClock


@pytest.fixture(scope="module")
def clock() -> MarketClock:
    return MarketClock()


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def test_default_calendar_is_the_nyse() -> None:
    assert DEFAULT_CALENDAR == "XNYS"
    assert MarketClock().name == "XNYS"


# --------------------------------------------------------------------------
# Closed: weekend, holiday, outside the session.
# --------------------------------------------------------------------------


def test_saturday_is_closed(clock: MarketClock) -> None:
    # 17:00 UTC = 13:00 ET, the middle of what would be a trading day.
    assert clock.is_open(_utc("2026-07-25T17:00")) is False


def test_sunday_is_closed(clock: MarketClock) -> None:
    assert clock.is_open(_utc("2026-07-26T17:00")) is False


def test_a_real_holiday_is_closed(clock: MarketClock) -> None:
    """New Year's Day 2026 is a Thursday -- a weekday check is not enough."""
    assert _utc("2026-01-01T17:00").weekday() < 5, "fixture must be a weekday"
    assert clock.is_open(_utc("2026-01-01T17:00")) is False


def test_before_the_open_and_after_the_close_are_closed(clock: MarketClock) -> None:
    assert clock.is_open(_utc("2026-07-21T12:00")) is False  # 08:00 ET, pre-market
    assert clock.is_open(_utc("2026-07-21T21:00")) is False  # 17:00 ET, after close


def test_an_ordinary_session_is_open(clock: MarketClock) -> None:
    assert clock.is_open(_utc("2026-07-21T15:00")) is True


# --------------------------------------------------------------------------
# The half day -- the case a hardcoded 16:00 gets wrong.
# --------------------------------------------------------------------------


def test_half_day_closes_early_while_a_normal_day_is_still_open(
    clock: MarketClock,
) -> None:
    """2026-11-27 closes 13:00 ET; 2026-11-25 does not.

    Same wall-clock minute, opposite answers. A hand-rolled 09:30-16:00 rule
    would report the half day as open for three more hours.
    """
    after_early_close = "T18:30"  # 13:30 ET
    assert clock.is_open(_utc(f"2026-11-27{after_early_close}")) is False
    assert clock.is_open(_utc(f"2026-11-25{after_early_close}")) is True
    # ...and the half day IS open before its early close.
    assert clock.is_open(_utc("2026-11-27T17:00")) is True


def test_half_day_close_comes_from_the_calendar(clock: MarketClock) -> None:
    close = clock.previous_close(_utc("2026-11-27T23:00"))
    assert close == _utc("2026-11-27T18:00")  # 13:00 ET


# --------------------------------------------------------------------------
# DST: the same ET close is a different UTC time in winter.
# --------------------------------------------------------------------------


def test_close_shifts_with_dst_without_any_hardcoded_offset(
    clock: MarketClock,
) -> None:
    summer_close = clock.previous_close(_utc("2026-07-21T23:00"))
    winter_close = clock.previous_close(_utc("2026-01-02T23:00"))
    assert summer_close == _utc("2026-07-21T20:00")  # 16:00 EDT
    assert winter_close == _utc("2026-01-02T21:00")  # 16:00 EST


def test_winter_session_is_still_open_after_the_summer_close_time(
    clock: MarketClock,
) -> None:
    """20:30 UTC is after the close in July and before it in January."""
    assert clock.is_open(_utc("2026-07-21T20:30")) is False
    assert clock.is_open(_utc("2026-01-02T20:30")) is True


# --------------------------------------------------------------------------
# previous_close semantics (what the snapshot job depends on).
# --------------------------------------------------------------------------


def test_previous_close_on_a_weekend_is_fridays_close(clock: MarketClock) -> None:
    assert clock.previous_close(_utc("2026-07-25T12:00")) == _utc("2026-07-24T20:00")


def test_previous_close_is_strictly_before_the_given_moment(
    clock: MarketClock,
) -> None:
    """At the exact close the session has not "closed since we looked" yet."""
    at_close = _utc("2026-07-21T20:00")
    assert clock.previous_close(at_close) == _utc("2026-07-20T20:00")
    assert clock.previous_close(at_close + timedelta(minutes=1)) == at_close


def test_previous_close_returns_an_aware_utc_datetime(clock: MarketClock) -> None:
    close = clock.previous_close(_utc("2026-07-21T23:00"))
    assert close is not None
    assert isinstance(close, datetime)
    assert close.tzinfo is not None
    assert close.utcoffset() == timedelta(0)


# --------------------------------------------------------------------------
# Fail closed: any question we cannot answer means "closed".
# --------------------------------------------------------------------------


def test_naive_datetime_is_refused_not_assumed_utc(clock: MarketClock) -> None:
    """exchange_calendars would silently localise this; we refuse instead.

    A naive datetime in the worker is a bug, and guessing its timezone turns
    that bug into a confident wrong answer about whether the market is open.
    """
    naive_mid_session = datetime(2026, 7, 21, 15, 0)  # noqa: DTZ001 -- the point
    assert clock.is_open(naive_mid_session) is False
    assert clock.previous_close(naive_mid_session) is None


def test_date_outside_the_calendar_bounds_fails_closed(clock: MarketClock) -> None:
    """Calendars are built over a bounded range and raise beyond it."""
    far_future = _utc("2099-07-21T15:00")
    assert clock.is_open(far_future) is False
    assert clock.previous_close(far_future) is None


def test_a_broken_calendar_reports_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any calendar failure at all -> closed, never an exception out of here."""

    class _Exploding:
        def is_open_on_minute(self, minute: object) -> bool:
            raise RuntimeError("calendar exploded")

        def previous_close(self, minute: object) -> object:
            raise RuntimeError("calendar exploded")

    broken = MarketClock()
    monkeypatch.setattr(broken, "_calendar", _Exploding())
    assert broken.is_open(_utc("2026-07-21T15:00")) is False
    assert broken.previous_close(_utc("2026-07-21T23:00")) is None


def test_unusable_close_value_becomes_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """A close we cannot turn into an aware datetime is None, not a guess."""

    class _Naive:
        def previous_close(self, minute: object) -> datetime:
            return datetime(2026, 7, 21, 20, 0)  # noqa: DTZ001 -- the point

    odd = MarketClock()
    monkeypatch.setattr(odd, "_calendar", _Naive())
    assert odd.previous_close(_utc("2026-07-21T23:00")) is None
