"""Tests for student-profile fields, stop times, and announcements.

Fixtures mirror real captures, with placeholder ids and text.
"""

from __future__ import annotations

import asyncio
import copy
from datetime import date, datetime, timedelta, timezone

from homeassistant.config_entries import ConfigEntry

from sf.api import (
    Announcement,
    StopfinderApi,
    _parse_announcements,
    _parse_students,
)
from sf.const import CONF_CLIENT_KEYS, CONF_SUBSCRIBER_ID, EVENT_ANNOUNCEMENT
from sf.coordinator import StopfinderCoordinator, _title_case

RIDER_ID = 1234567
DAY = date(2026, 8, 11)

STUDENTS = [
    {
        "date": "2026-08-11T00:00:00",
        "studentSchedules": [
            {
                "subscriptionOwner": True,
                "riderId": RIDER_ID,
                "firstName": "Test",
                "lastName": "Student",
                "grade": "04",
                "school": "Southside Elementary School (392)",
                "clientId": 577,
                "dataSourceId": 45,
                "timeZoneMinutes": -240.0,
                "displayVehicleOnMap": True,
                "beforeTrip": 15,
                "afterTrip": 15,
                "trips": [
                    {
                        "id": 303,
                        "name": "233 AM 2 SSIDE",
                        "busNumber": "233",
                        "vehicleId": 100,
                        "toSchool": True,
                        "pickUpStopName": "Elm St & 3rd",
                        "pickUpTime": "2026-08-11T07:40:47",
                        "dropOffStopName": "Southside Elementary School",
                        "dropOffTime": "2026-08-11T08:01:10",
                        "startTime": "2026-08-11T07:21:29",
                        "finishTime": "2026-08-11T08:23:26",
                        "pickUpStopId": 5066,
                        "dropOffStopId": 5050,
                        "adjustMinutes": 0,
                    },
                    {
                        "id": 77,
                        "name": "233 PM 1 SSIDE",
                        "busNumber": "233",
                        "vehicleId": 100,
                        "toSchool": False,
                        "pickUpStopName": "Southside Elementary School",
                        "pickUpTime": "2026-08-11T14:30:00",
                        "dropOffStopName": "Elm St & 3rd",
                        "dropOffTime": "2026-08-11T14:52:49",
                        "startTime": "2026-08-11T14:16:42",
                        "finishTime": "2026-08-11T15:12:09",
                        "pickUpStopId": 1034,
                        "dropOffStopId": 1045,
                        "adjustMinutes": 0,
                    },
                ],
            }
        ],
    }
]

ANNOUNCEMENTS = [
    {
        "id": 555,
        "clientId": 577,
        "name": "233 running 20 mins late - Bus 242",
        "description": "233 running 20 mins late - Bus 242",
        "subject": "233 running 20 mins late - Bus 242",
        "body": "Bus 233 running 20 mins late. 0745 11/10/2025",
        "pushed": False,
        "sentByName": None,
        "sentOn": "2025-11-10T12:46:56.94",
        "openedOn": "2025-11-10T19:57:15",
        "read": True,
        "archived": True,
        "Id": 555,
    }
]


class TestStudentProfile:
    def test_profile_fields_are_parsed(self) -> None:
        [sched] = _parse_students(STUDENTS, DAY)

        assert sched.full_name == "Test Student"
        assert sched.grade == "04"
        assert sched.school == "Southside Elementary School (392)"

    def test_home_stop_is_the_students_end_of_the_route(self) -> None:
        """Pickup on the way to school, dropoff on the way back — same stop."""
        [sched] = _parse_students(STUDENTS, DAY)
        assert sched.home_stop == "Elm St & 3rd"

    def test_home_stop_from_an_afternoon_only_schedule(self) -> None:
        payload = copy.deepcopy(STUDENTS)
        payload[0]["studentSchedules"][0]["trips"] = [
            payload[0]["studentSchedules"][0]["trips"][1]
        ]
        [sched] = _parse_students(payload, DAY)
        assert sched.home_stop == "Elm St & 3rd"

    def test_missing_grade_is_empty_not_none(self) -> None:
        payload = copy.deepcopy(STUDENTS)
        del payload[0]["studentSchedules"][0]["grade"]
        [sched] = _parse_students(payload, DAY)
        assert sched.grade == ""


class TestStopTimes:
    def test_pickup_and_dropoff_times_are_district_local(self) -> None:
        [sched] = _parse_students(STUDENTS, DAY)
        am = sched.trips[0]

        # These are the times the bus reaches the student's own stop, distinct
        # from startTime/finishTime which bracket the whole route.
        assert am.pickup_time.isoformat() == "2026-08-11T07:40:47-04:00"
        assert am.dropoff_time.isoformat() == "2026-08-11T08:01:10-04:00"
        assert am.start_time.isoformat() == "2026-08-11T07:21:29-04:00"

    def test_adjust_minutes_shifts_the_stop_times(self) -> None:
        payload = copy.deepcopy(STUDENTS)
        payload[0]["studentSchedules"][0]["trips"][0]["adjustMinutes"] = 10
        [sched] = _parse_students(payload, DAY)

        assert sched.trips[0].adjusted_pickup_time.isoformat() == (
            "2026-08-11T07:50:47-04:00"
        )

    def test_absent_stop_times_stay_none(self) -> None:
        payload = copy.deepcopy(STUDENTS)
        del payload[0]["studentSchedules"][0]["trips"][0]["pickUpTime"]
        [sched] = _parse_students(payload, DAY)

        assert sched.trips[0].pickup_time is None
        assert sched.trips[0].adjusted_pickup_time is None


class TestNextStopTimes:
    def _state(self):
        from sf.coordinator import RiderState

        [sched] = _parse_students(STUDENTS, DAY)
        return RiderState(schedule=sched)

    def test_morning_picks_the_am_trip(self) -> None:
        # 06:00 EDT, before either pickup.
        now = datetime(2026, 8, 11, 10, 0, tzinfo=timezone.utc)
        assert self._state().next_pickup(now).trip_id == 303

    def test_midday_rolls_on_to_the_pm_trip(self) -> None:
        # 12:00 EDT, after the morning pickup, before the afternoon one.
        now = datetime(2026, 8, 11, 16, 0, tzinfo=timezone.utc)
        state = self._state()
        assert state.next_pickup(now).trip_id == 77
        assert state.next_dropoff(now).trip_id == 77

    def test_after_the_last_trip_there_is_no_answer(self) -> None:
        """The roster we hold covers today, so tomorrow is not ours to guess."""
        now = datetime(2026, 8, 11, 23, 0, tzinfo=timezone.utc)
        state = self._state()
        assert state.next_pickup(now) is None
        assert state.next_dropoff(now) is None


class TestParseAnnouncements:
    def test_parses_an_announcement(self) -> None:
        [item] = _parse_announcements(ANNOUNCEMENTS)

        assert item.announcement_id == "555"
        assert item.subject == "233 running 20 mins late - Bus 242"
        assert item.read is True
        assert item.archived is True

    def test_sent_on_is_utc(self) -> None:
        """The body says "0745"; as UTC that is 07:46 in this UTC-5 district."""
        [item] = _parse_announcements(ANNOUNCEMENTS)

        assert item.sent_on == datetime(
            2025, 11, 10, 12, 46, 56, 940000, tzinfo=timezone.utc
        )
        local = item.sent_on.astimezone(timezone(timedelta(hours=-5)))
        assert (local.hour, local.minute) == (7, 46)

    def test_newest_first(self) -> None:
        older = copy.deepcopy(ANNOUNCEMENTS[0])
        older["id"] = older["Id"] = 111
        older["sentOn"] = "2025-09-01T12:00:00"
        newer = copy.deepcopy(ANNOUNCEMENTS[0])
        newer["id"] = newer["Id"] = 999
        newer["sentOn"] = "2026-08-01T12:00:00"

        ids = [a.announcement_id for a in _parse_announcements([older, newer])]
        assert ids == ["999", "111"]

    def test_undated_announcements_sort_last_without_erroring(self) -> None:
        undated = copy.deepcopy(ANNOUNCEMENTS[0])
        undated["id"] = undated["Id"] = 111
        undated["sentOn"] = None
        also_undated = copy.deepcopy(undated)
        also_undated["id"] = also_undated["Id"] = 222

        parsed = _parse_announcements([undated, also_undated, ANNOUNCEMENTS[0]])
        assert parsed[0].announcement_id == "555"
        assert len(parsed) == 3

    def test_records_without_an_id_are_skipped(self) -> None:
        payload = copy.deepcopy(ANNOUNCEMENTS)
        del payload[0]["id"]
        del payload[0]["Id"]
        assert _parse_announcements(payload) == []

    def test_empty_and_missing(self) -> None:
        assert _parse_announcements([]) == []
        assert _parse_announcements(None) == []


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
    def __init__(self, responses: list[list[Announcement]]) -> None:
        self._responses = list(responses)
        self.calls = 0
        self.client_id = "bartholomew"
        self.client_keys = "bartholomew"

    async def fetch_announcements(self):
        self.calls += 1
        return self._responses.pop(0) if self._responses else []


def _coordinator(responses=None) -> StopfinderCoordinator:
    entry = ConfigEntry(
        data={CONF_SUBSCRIBER_ID: 2058262, CONF_CLIENT_KEYS: "bartholomew"}
    )
    coordinator = StopfinderCoordinator(hass=_FakeHass(), entry=entry)
    coordinator.api = _FakeApi(responses or [])
    return coordinator


class TestAnnouncementPolling:
    def test_first_poll_primes_without_firing(self) -> None:
        """Last year's notice is a normal response; it must not raise an event."""
        [stale] = _parse_announcements(ANNOUNCEMENTS)
        coordinator = _coordinator(responses=[[stale]])

        asyncio.run(coordinator._async_poll_announcements(_NOW))

        assert coordinator.hass.bus.events == []
        # ...but it is still shown, so its age is visible.
        assert coordinator.announcement is stale

    def test_a_new_announcement_fires_once(self) -> None:
        [stale] = _parse_announcements(ANNOUNCEMENTS)
        fresh_payload = copy.deepcopy(ANNOUNCEMENTS[0])
        fresh_payload["id"] = fresh_payload["Id"] = 999
        fresh_payload["sentOn"] = "2026-08-11T11:30:00"
        [fresh] = _parse_announcements([fresh_payload])

        coordinator = _coordinator(
            responses=[[stale], [fresh, stale], [fresh, stale]]
        )

        asyncio.run(coordinator._async_poll_announcements(_NOW))
        asyncio.run(coordinator._async_poll_announcements(_NOW))
        asyncio.run(coordinator._async_poll_announcements(_NOW))

        assert len(coordinator.hass.bus.events) == 1
        event_type, data = coordinator.hass.bus.events[0]
        assert event_type == EVENT_ANNOUNCEMENT
        assert data["announcement_id"] == "999"
        assert coordinator.announcement.announcement_id == "999"

    def test_district_is_cased_for_display(self) -> None:
        coordinator = _coordinator()
        assert coordinator.district == "Bartholomew"

    def test_district_falls_back_to_the_stored_key(self) -> None:
        """After a restart the entry's copy is all we have until identity loads."""
        coordinator = _coordinator()
        coordinator.api.client_id = ""
        assert coordinator.district == "Bartholomew"


class TestAnnouncementThrottle:
    def test_due_before_the_first_poll(self) -> None:
        assert _coordinator()._announcements_due(_NOW)

    def test_not_due_again_immediately(self) -> None:
        coordinator = _coordinator(responses=[[]])
        asyncio.run(coordinator._async_poll_announcements(_NOW))

        assert not coordinator._announcements_due(_NOW + timedelta(minutes=1))
        assert coordinator._announcements_due(_NOW + timedelta(minutes=15))

    def test_the_option_changes_the_interval(self) -> None:
        coordinator = _coordinator(responses=[[]])
        coordinator.entry.options = {"announcement_poll_minutes": 60}
        asyncio.run(coordinator._async_poll_announcements(_NOW))

        assert not coordinator._announcements_due(_NOW + timedelta(minutes=30))
        assert coordinator._announcements_due(_NOW + timedelta(minutes=60))


_NOW = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)


class TestDistrictCasing:
    def test_lowercase_keys_are_cased_for_reading(self) -> None:
        assert _title_case("bartholomew") == "Bartholomew"

    def test_acronyms_are_left_alone(self) -> None:
        """Blind .title() would turn "BCSC" into "Bcsc"."""
        assert _title_case("BCSC") == "BCSC"
        assert _title_case("McTown") == "McTown"

    def test_multiword_keys(self) -> None:
        assert _title_case("north-central") == "North-Central"
        assert _title_case("st. marys") == "St. Marys"

    def test_multi_district_keys_are_cased_individually(self) -> None:
        """A parent with children in two districts gets a comma-joined key."""
        assert _title_case("bartholomew,BCSC") == "Bartholomew, BCSC"

    def test_empty_stays_empty(self) -> None:
        assert _title_case("") == ""

    def test_the_header_still_goes_out_lowercase(self) -> None:
        """Display casing must not leak into x-client-keys, which the app lowercases."""
        api = StopfinderApi(session=None)
        api.client_id = "BCSC"
        api.client_keys = "bcsc"

        assert api._base_headers()["x-client-keys"] == "bcsc"
