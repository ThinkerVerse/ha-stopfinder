"""Tests for geo-alert notification parsing and new-alert detection.

The response fixture mirrors a real capture, with placeholder ids and text.
"""

from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timezone

from homeassistant.config_entries import ConfigEntry

from sf.api import GeoAlert, StudentSchedule, Trip, _parse_geo_alerts, _parse_utc
from sf.const import CONF_SUBSCRIBER_ID, EVENT_GEO_ALERT
from sf.coordinator import (
    RiderState,
    StopfinderCoordinator,
    _is_newer,
)

RIDER_ID = 1234567
TRIP_ID = 303
SUBSCRIBER_ID = 2058262
DATA_SOURCE_ID = 12

RESPONSE = [
    {
        "riderId": RIDER_ID,
        "subscriberId": SUBSCRIBER_ID,
        "tripId": TRIP_ID,
        "geoAlertNotification": {
            "id": 987654,
            "subscriberId": SUBSCRIBER_ID,
            "subscriberFirstName": None,
            "subscriberLastName": None,
            "studentId": 0,
            "riderId": RIDER_ID,
            "riderName": None,
            "body": "The bus is arriving at Spring Hill",
            "subject": "Test Student",
            "name": "Spring Hill",
            "alertType": True,
            "sentOn": "2026-08-11T11:30:12.6332402",
            "createTime": "2026-08-11T11:30:12.4762249",
            "tripId": TRIP_ID,
            "tripName": None,
            "dataSourceId": DATA_SOURCE_ID,
            "dataSourceName": None,
            "rider": None,
            "Id": 987654,
        },
        "dataSourceId": DATA_SOURCE_ID,
    }
]


def _alert(alert_id: str, sent_on: datetime | None) -> GeoAlert:
    return GeoAlert(
        alert_id=alert_id,
        rider_id=RIDER_ID,
        trip_id=TRIP_ID,
        zone_name="Spring Hill",
        subject="Test Student",
        body="…",
        sent_on=sent_on,
        created_at=None,
        alert_type=True,
        raw={},
    )


class TestParseGeoAlerts:
    def test_parses_a_notification(self) -> None:
        [alert] = _parse_geo_alerts(RESPONSE)

        assert alert.alert_id == "987654"
        assert alert.rider_id == RIDER_ID
        assert alert.trip_id == TRIP_ID
        assert alert.zone_name == "Spring Hill"
        assert alert.subject == "Test Student"
        assert alert.body == "The bus is arriving at Spring Hill"
        assert alert.alert_type is True

    def test_timestamps_are_utc(self) -> None:
        [alert] = _parse_geo_alerts(RESPONSE)

        assert alert.sent_on == datetime(
            2026, 8, 11, 11, 30, 12, 633240, tzinfo=timezone.utc
        )
        assert alert.created_at is not None
        assert alert.created_at.tzinfo is timezone.utc

    def test_entries_without_a_notification_are_skipped(self) -> None:
        payload = copy.deepcopy(RESPONSE)
        payload[0]["geoAlertNotification"] = None
        assert _parse_geo_alerts(payload) == []

        payload = copy.deepcopy(RESPONSE)
        del payload[0]["geoAlertNotification"]
        assert _parse_geo_alerts(payload) == []

    def test_empty_and_missing_responses(self) -> None:
        assert _parse_geo_alerts([]) == []
        assert _parse_geo_alerts(None) == []

    def test_notification_without_an_id_is_skipped(self) -> None:
        """Without an id a repeat is indistinguishable from a new alert."""
        payload = copy.deepcopy(RESPONSE)
        del payload[0]["geoAlertNotification"]["id"]
        del payload[0]["geoAlertNotification"]["Id"]
        assert _parse_geo_alerts(payload) == []

    def test_falls_back_to_the_capitalised_id(self) -> None:
        payload = copy.deepcopy(RESPONSE)
        del payload[0]["geoAlertNotification"]["id"]
        [alert] = _parse_geo_alerts(payload)
        assert alert.alert_id == "987654"

    def test_string_ids_are_coerced(self) -> None:
        payload = copy.deepcopy(RESPONSE)
        payload[0]["riderId"] = str(RIDER_ID)
        payload[0]["tripId"] = str(TRIP_ID)
        [alert] = _parse_geo_alerts(payload)
        assert alert.rider_id == RIDER_ID
        assert alert.trip_id == TRIP_ID


class TestParseUtc:
    def test_handles_dotnet_seven_digit_fractions(self) -> None:
        assert _parse_utc("2026-08-11T11:30:12.6332402") == datetime(
            2026, 8, 11, 11, 30, 12, 633240, tzinfo=timezone.utc
        )

    def test_handles_a_plain_timestamp(self) -> None:
        assert _parse_utc("2026-08-11T11:30:12") == datetime(
            2026, 8, 11, 11, 30, 12, tzinfo=timezone.utc
        )

    def test_respects_an_explicit_offset(self) -> None:
        assert _parse_utc("2026-08-11T07:30:12-04:00") == datetime(
            2026, 8, 11, 11, 30, 12, tzinfo=timezone.utc
        )

    def test_junk_returns_none(self) -> None:
        assert _parse_utc(None) is None
        assert _parse_utc("") is None
        assert _parse_utc("not a date") is None
        assert _parse_utc("not a date.123") is None


class TestIsNewer:
    def test_first_alert_always_wins(self) -> None:
        assert _is_newer(_alert("1", None), None)

    def test_the_same_alert_does_not_replace_itself(self) -> None:
        """The endpoint repeats the latest alert on every poll."""
        current = _alert("1", datetime(2026, 8, 11, 11, 30, tzinfo=timezone.utc))
        assert not _is_newer(_alert("1", current.sent_on), current)

    def test_a_later_alert_replaces_an_earlier_one(self) -> None:
        current = _alert("1", datetime(2026, 8, 11, 11, 30, tzinfo=timezone.utc))
        newer = _alert("2", datetime(2026, 8, 11, 11, 45, tzinfo=timezone.utc))
        assert _is_newer(newer, current)

    def test_an_older_alert_does_not_replace_a_newer_one(self) -> None:
        current = _alert("2", datetime(2026, 8, 11, 11, 45, tzinfo=timezone.utc))
        older = _alert("1", datetime(2026, 8, 11, 11, 30, tzinfo=timezone.utc))
        assert not _is_newer(older, current)

    def test_missing_timestamps_fall_back_to_the_newer_id(self) -> None:
        current = _alert("1", None)
        assert _is_newer(_alert("2", None), current)


# --------------------------------------------------------------------------
# coordinator: payload construction, priming, and event dedupe
# --------------------------------------------------------------------------


class _FakeBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def async_fire(self, event_type: str, data: dict) -> None:
        self.events.append((event_type, data))


class _FakeHass:
    def __init__(self) -> None:
        self.bus = _FakeBus()
        self.config = type("Config", (), {"language": "en"})()


class _FakeApi:
    """Stands in for the REST client, recording what it was asked for."""

    def __init__(self, responses: list[list[GeoAlert]]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[int, list[dict], str]] = []

    async def fetch_geo_alerts(self, subscriber_id, requests, language):
        self.calls.append((subscriber_id, requests, language))
        return self._responses.pop(0) if self._responses else []


def _schedule(rider_id: int, trips: list[Trip], data_source_id: int = DATA_SOURCE_ID):
    return StudentSchedule(
        rider_id=rider_id,
        first_name="Test",
        last_name="Student",
        school="Example Elementary",
        client_id=999,
        data_source_id=data_source_id,
        display_vehicle_on_map=True,
        tz_offset_minutes=-240.0,
        trips=trips,
    )


def _trip(trip_id: int) -> Trip:
    now = datetime(2026, 8, 11, 11, 0, tzinfo=timezone.utc)
    return Trip(
        trip_id=trip_id,
        name=f"trip {trip_id}",
        bus_number="42",
        vehicle_id=100,
        to_school=True,
        start_time=now,
        finish_time=now,
        pickup_stop_id=1,
        dropoff_stop_id=2,
        pickup_stop_name="",
        dropoff_stop_name="",
        before_trip_min=15,
        after_trip_min=15,
    )


def _coordinator(responses=None, riders=None) -> StopfinderCoordinator:
    entry = ConfigEntry(data={CONF_SUBSCRIBER_ID: SUBSCRIBER_ID})
    coordinator = StopfinderCoordinator(hass=_FakeHass(), entry=entry)
    coordinator.api = _FakeApi(responses or [])
    coordinator.data = riders if riders is not None else {
        RIDER_ID: RiderState(schedule=_schedule(RIDER_ID, [_trip(TRIP_ID)]))
    }
    return coordinator


class TestGeoAlertRequests:
    def test_one_entry_per_rider_and_trip(self) -> None:
        riders = {
            RIDER_ID: RiderState(
                schedule=_schedule(RIDER_ID, [_trip(303), _trip(77)])
            )
        }
        assert _coordinator(riders=riders)._geo_alert_requests() == [
            {
                "riderId": RIDER_ID,
                "subscriberId": SUBSCRIBER_ID,
                "tripId": 303,
                "dataSourceId": DATA_SOURCE_ID,
            },
            {
                "riderId": RIDER_ID,
                "subscriberId": SUBSCRIBER_ID,
                "tripId": 77,
                "dataSourceId": DATA_SOURCE_ID,
            },
        ]

    def test_riders_without_trips_or_data_source_are_skipped(self) -> None:
        riders = {
            1: RiderState(schedule=_schedule(1, [])),
            2: RiderState(schedule=_schedule(2, [_trip(1)], data_source_id=0)),
        }
        assert _coordinator(riders=riders)._geo_alert_requests() == []

    def test_no_subscriber_id_means_no_request(self) -> None:
        coordinator = _coordinator()
        coordinator.entry.data = {}
        assert coordinator._geo_alert_requests() == []


class TestGeoAlertPolling:
    def test_first_poll_primes_without_firing(self) -> None:
        """A restart must not replay an alert that fired while HA was down."""
        alert = _alert("1", datetime(2026, 8, 11, 11, 30, tzinfo=timezone.utc))
        coordinator = _coordinator(responses=[[alert]])

        asyncio.run(coordinator._async_poll_geo_alerts(_NOW))

        assert coordinator.hass.bus.events == []
        # ...but the alert is still shown on the entity.
        assert coordinator.data[RIDER_ID].geo_alert is alert

    def test_a_new_alert_after_priming_fires_once(self) -> None:
        first = _alert("1", datetime(2026, 8, 11, 11, 30, tzinfo=timezone.utc))
        second = _alert("2", datetime(2026, 8, 11, 11, 45, tzinfo=timezone.utc))
        coordinator = _coordinator(responses=[[first], [second], [second]])

        asyncio.run(coordinator._async_poll_geo_alerts(_NOW))  # primes
        asyncio.run(coordinator._async_poll_geo_alerts(_NOW))  # new -> fires
        asyncio.run(coordinator._async_poll_geo_alerts(_NOW))  # repeat -> silent

        assert len(coordinator.hass.bus.events) == 1
        event_type, data = coordinator.hass.bus.events[0]
        assert event_type == EVENT_GEO_ALERT
        assert data["alert_id"] == "2"
        assert data["zone"] == "Spring Hill"
        assert data["rider_id"] == RIDER_ID
        assert data["student"] == "Test Student"
        assert data["sent_on"] == second.sent_on.isoformat()

    def test_alerts_for_unknown_riders_are_ignored(self) -> None:
        stray = GeoAlert(
            alert_id="9",
            rider_id=999999,
            trip_id=TRIP_ID,
            zone_name="Elsewhere",
            subject="",
            body="",
            sent_on=None,
            created_at=None,
            alert_type=True,
            raw={},
        )
        coordinator = _coordinator(responses=[[stray], [stray]])

        asyncio.run(coordinator._async_poll_geo_alerts(_NOW))
        asyncio.run(coordinator._async_poll_geo_alerts(_NOW))

        assert coordinator.hass.bus.events == []
        assert coordinator.data[RIDER_ID].geo_alert is None

    def test_nothing_is_requested_without_a_payload(self) -> None:
        coordinator = _coordinator(riders={})
        asyncio.run(coordinator._async_poll_geo_alerts(_NOW))
        assert coordinator.api.calls == []

    def test_the_configured_language_is_sent(self) -> None:
        coordinator = _coordinator(responses=[[]])
        asyncio.run(coordinator._async_poll_geo_alerts(_NOW))

        subscriber_id, requests, language = coordinator.api.calls[0]
        assert subscriber_id == SUBSCRIBER_ID
        assert language == "en"
        assert len(requests) == 1


_NOW = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
