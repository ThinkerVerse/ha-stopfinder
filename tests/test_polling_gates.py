"""Pins the promise that nothing is requested outside a trip window.

Two timers run: a slow schedule tick that only computes window state, and an
in-window timer that carries every recurring request. These tests assert the
division, since a stray call on the slow tick would be invisible in normal use
but would poll the API around the clock.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

from homeassistant.config_entries import ConfigEntry

from sf.api import GpsFix, StudentSchedule, Trip
from sf.const import (
    CONF_ANNOUNCEMENT_POLL_MINUTES,
    CONF_GEO_ALERT_POLL_SECONDS,
    CONF_SUBSCRIBER_ID,
    GEO_ALERT_POLL_SECONDS,
)
from sf.coordinator import RiderState, StopfinderCoordinator

RIDER_ID = 1234567
NOW = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)


class _CountingApi:
    """Records every endpoint the coordinator reaches for."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.client_id = "bartholomew"
        self.client_keys = "bartholomew"

    async def fetch_gps(self, group):
        self.calls.append("gps")
        return GpsFix(latitude=39.1, longitude=-85.9, fix_time=NOW, raw={})

    async def fetch_geo_alerts(self, subscriber_id, requests, language):
        self.calls.append("geo_alerts")
        return []

    async def fetch_announcements(self):
        self.calls.append("announcements")
        return []

    async def fetch_students(self, day=None):
        self.calls.append("students")
        return []

    async def fetch_client_identity(self):
        self.calls.append("apiversions")
        return {}


class _FakeBus:
    def async_fire(self, event_type, data) -> None:
        pass


class _FakeHass:
    def __init__(self) -> None:
        self.bus = _FakeBus()
        self.config = type("Config", (), {"language": "en"})()


def _trip(*, start: datetime, finish: datetime) -> Trip:
    return Trip(
        trip_id=303,
        name="233 AM",
        bus_number="233",
        vehicle_id=100,
        to_school=True,
        start_time=start,
        finish_time=finish,
        pickup_stop_id=1,
        dropoff_stop_id=2,
        pickup_stop_name="Elm St & 3rd",
        dropoff_stop_name="School",
        before_trip_min=15,
        after_trip_min=15,
    )


def _coordinator(*, trip: Trip, options: dict | None = None):
    entry = ConfigEntry(
        data={CONF_SUBSCRIBER_ID: 2058262}, options=options or {}
    )
    coordinator = StopfinderCoordinator(hass=_FakeHass(), entry=entry)
    coordinator.api = _CountingApi()
    coordinator.data = {
        RIDER_ID: RiderState(
            schedule=StudentSchedule(
                rider_id=RIDER_ID,
                first_name="Test",
                last_name="Student",
                school="School",
                client_id=577,
                data_source_id=45,
                display_vehicle_on_map=True,
                tz_offset_minutes=-240.0,
                trips=[trip],
            )
        )
    }
    # Pretend the roster is already current, so the rollover fetch is not due.
    coordinator._schedule_day = date.today()

    async def _no_auth_needed() -> None:
        return None

    coordinator._ensure_token = _no_auth_needed
    return coordinator


# A window that is closed at NOW, and one that is open.
CLOSED_TRIP = _trip(
    start=NOW + timedelta(hours=5), finish=NOW + timedelta(hours=6)
)
OPEN_TRIP = _trip(start=NOW - timedelta(minutes=5), finish=NOW + timedelta(minutes=30))


class TestOutsideAWindow:
    def test_the_schedule_tick_makes_no_request(self) -> None:
        coordinator = _coordinator(trip=CLOSED_TRIP)

        asyncio.run(coordinator._async_schedule_tick(NOW))

        assert coordinator.api.calls == []

    def test_no_in_window_timer_is_armed(self) -> None:
        coordinator = _coordinator(trip=CLOSED_TRIP)

        asyncio.run(coordinator._async_schedule_tick(NOW))

        assert coordinator._unsub_gps is None

    def test_repeated_ticks_stay_silent(self) -> None:
        """A whole quiet day of ticks must not accumulate any traffic."""
        coordinator = _coordinator(trip=CLOSED_TRIP)

        for _ in range(60):
            asyncio.run(coordinator._async_schedule_tick(NOW))

        assert coordinator.api.calls == []

    def test_the_in_window_tick_is_a_no_op_if_it_somehow_fires(self) -> None:
        coordinator = _coordinator(trip=CLOSED_TRIP)

        asyncio.run(coordinator._async_gps_tick(NOW))

        assert coordinator.api.calls == []


class TestInsideAWindow:
    def test_opening_a_window_fetches_everything_at_once(self) -> None:
        """No waiting out an interval at the moment the bus starts running."""
        coordinator = _coordinator(trip=OPEN_TRIP)

        asyncio.run(coordinator._async_schedule_tick(NOW))

        assert coordinator.api.calls == ["gps", "geo_alerts", "announcements"]
        assert coordinator._unsub_gps is not None

    def test_later_ticks_poll_gps_but_throttle_the_rest(self) -> None:
        coordinator = _coordinator(trip=OPEN_TRIP)
        asyncio.run(coordinator._async_schedule_tick(NOW))
        coordinator.api.calls.clear()

        # One second later: GPS is due every tick, the others are not.
        asyncio.run(coordinator._async_gps_tick(NOW + timedelta(seconds=1)))

        assert coordinator.api.calls == ["gps"]

    def test_geo_alerts_come_round_again_on_their_own_interval(self) -> None:
        coordinator = _coordinator(trip=OPEN_TRIP)
        asyncio.run(coordinator._async_schedule_tick(NOW))
        coordinator.api.calls.clear()

        later = NOW + timedelta(seconds=GEO_ALERT_POLL_SECONDS)
        asyncio.run(coordinator._async_gps_tick(later))

        assert coordinator.api.calls == ["gps", "geo_alerts"]

    def test_the_geo_alert_interval_is_configurable(self) -> None:
        coordinator = _coordinator(
            trip=OPEN_TRIP, options={CONF_GEO_ALERT_POLL_SECONDS: 120}
        )
        asyncio.run(coordinator._async_schedule_tick(NOW))
        coordinator.api.calls.clear()

        asyncio.run(coordinator._async_gps_tick(NOW + timedelta(seconds=30)))
        assert coordinator.api.calls == ["gps"]

        asyncio.run(coordinator._async_gps_tick(NOW + timedelta(seconds=120)))
        assert coordinator.api.calls == ["gps", "gps", "geo_alerts"]

    def test_announcements_use_their_slower_clock(self) -> None:
        coordinator = _coordinator(
            trip=OPEN_TRIP, options={CONF_ANNOUNCEMENT_POLL_MINUTES: 15}
        )
        asyncio.run(coordinator._async_schedule_tick(NOW))
        coordinator.api.calls.clear()

        asyncio.run(coordinator._async_gps_tick(NOW + timedelta(minutes=14)))
        assert "announcements" not in coordinator.api.calls

        asyncio.run(coordinator._async_gps_tick(NOW + timedelta(minutes=15)))
        assert "announcements" in coordinator.api.calls


class TestWindowClosing:
    def test_closing_disarms_the_timer(self) -> None:
        coordinator = _coordinator(trip=OPEN_TRIP)
        asyncio.run(coordinator._async_schedule_tick(NOW))
        assert coordinator._unsub_gps is not None

        # Two hours on, the window has closed.
        asyncio.run(coordinator._async_schedule_tick(NOW + timedelta(hours=2)))

        assert coordinator._unsub_gps is None

    def test_reopening_fetches_immediately_again(self) -> None:
        coordinator = _coordinator(trip=OPEN_TRIP)
        asyncio.run(coordinator._async_schedule_tick(NOW))
        coordinator._stop_gps_polling()
        coordinator.api.calls.clear()

        asyncio.run(coordinator._async_schedule_tick(NOW))

        assert coordinator.api.calls == ["gps", "geo_alerts", "announcements"]
