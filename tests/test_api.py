"""Tests for the REST client's parsing and window logic.

Payloads mirror the shapes observed in real traffic, with placeholder ids.
"""

from __future__ import annotations

import base64
import json
from datetime import date, datetime, timedelta, timezone

from sf.api import GpsFix, _jwt_exp, _parse_gps, _parse_students

CLIENT_ID = 999
DATA_SOURCE_ID = 12
BUS = "42"
RIDER_ID = 1234567

# 2026-08-10T12:00:08Z, i.e. 08:00:08 in a -04:00 district.
GPS_EPOCH = 1786363208


def _students_payload(*, adjust_minutes: int = 0, bus_number: str = BUS) -> list[dict]:
    return [
        {
            "date": "2026-08-10T00:00:00",
            "studentSchedules": [
                {
                    "riderId": RIDER_ID,
                    "firstName": "Test",
                    "lastName": "Student",
                    "school": "Example Elementary",
                    "clientId": CLIENT_ID,
                    "dataSourceId": DATA_SOURCE_ID,
                    "timeZoneMinutes": -240.0,
                    "displayVehicleOnMap": True,
                    "beforeTrip": 15,
                    "afterTrip": 15,
                    "trips": [
                        {
                            "id": 303,
                            "name": "42 AM 2 SSIDE",
                            "busNumber": bus_number,
                            "vehicleId": 100,
                            "toSchool": True,
                            "startTime": "2026-08-10T07:21:29",
                            "finishTime": "2026-08-10T08:23:26",
                            "pickUpStopId": 5066,
                            "dropOffStopId": 5050,
                            "pickUpStopName": "Elm St & 3rd",
                            "dropOffStopName": "Example Elementary",
                            "adjustMinutes": adjust_minutes,
                        }
                    ],
                }
            ],
        }
    ]


class TestParseStudents:
    def test_group_name_and_timezone(self) -> None:
        [sched] = _parse_students(_students_payload(), date(2026, 8, 10))
        trip = sched.trips[0]

        assert sched.group_name(trip) == f"{CLIENT_ID}_{DATA_SOURCE_ID}_{BUS}"
        # Naive district-local times get timeZoneMinutes attached, not converted.
        assert trip.start_time.utcoffset() == timedelta(minutes=-240)
        assert trip.start_time.isoformat() == "2026-08-10T07:21:29-04:00"
        assert trip.pickup_stop_name == "Elm St & 3rd"
        assert trip.dropoff_stop_name == "Example Elementary"

    def test_other_days_are_ignored(self) -> None:
        assert _parse_students(_students_payload(), date(2026, 8, 11)) == []

    def test_no_service_day_is_not_an_error(self) -> None:
        payload = _students_payload()
        payload[0]["studentSchedules"][0]["trips"] = []
        [sched] = _parse_students(payload, date(2026, 8, 10))
        assert sched.trips == []
        assert sched.rider_id == RIDER_ID

    def test_missing_adjust_minutes_defaults_to_zero(self) -> None:
        payload = _students_payload()
        del payload[0]["studentSchedules"][0]["trips"][0]["adjustMinutes"]
        [sched] = _parse_students(payload, date(2026, 8, 10))
        assert sched.trips[0].adjust_minutes == 0

    def test_null_adjust_minutes_defaults_to_zero(self) -> None:
        payload = _students_payload()
        payload[0]["studentSchedules"][0]["trips"][0]["adjustMinutes"] = None
        [sched] = _parse_students(payload, date(2026, 8, 10))
        assert sched.trips[0].adjust_minutes == 0


class TestTripWindow:
    def test_window_applies_before_and_after_padding(self) -> None:
        [sched] = _parse_students(_students_payload(), date(2026, 8, 10))
        trip = sched.trips[0]

        assert trip.window_start.isoformat() == "2026-08-10T07:06:29-04:00"
        assert trip.window_end.isoformat() == "2026-08-10T08:38:26-04:00"

    def test_adjust_minutes_shifts_both_ends(self) -> None:
        [sched] = _parse_students(
            _students_payload(adjust_minutes=30), date(2026, 8, 10)
        )
        trip = sched.trips[0]

        # adjustMinutes lands before the padding, so both ends move by 30.
        assert trip.window_start.isoformat() == "2026-08-10T07:36:29-04:00"
        assert trip.window_end.isoformat() == "2026-08-10T09:08:26-04:00"

    def test_is_running_is_inclusive_of_both_bounds(self) -> None:
        [sched] = _parse_students(_students_payload(), date(2026, 8, 10))
        trip = sched.trips[0]

        assert trip.is_running(trip.window_start)
        assert trip.is_running(trip.window_end)
        assert not trip.is_running(trip.window_start - timedelta(seconds=1))
        assert not trip.is_running(trip.window_end + timedelta(seconds=1))

    def test_adjust_minutes_changes_whether_a_trip_is_running(self) -> None:
        moment = datetime(2026, 8, 10, 13, 0, 0, tzinfo=timezone.utc)  # 09:00 EDT

        [plain] = _parse_students(_students_payload(), date(2026, 8, 10))
        [shifted] = _parse_students(
            _students_payload(adjust_minutes=30), date(2026, 8, 10)
        )

        # Without the shift the window has closed; with it, the bus is still out.
        assert not plain.trips[0].is_running(moment)
        assert shifted.trips[0].is_running(moment)

    def test_has_vehicle_reflects_bus_assignment(self) -> None:
        [assigned] = _parse_students(_students_payload(), date(2026, 8, 10))
        [unassigned] = _parse_students(
            _students_payload(bus_number=""), date(2026, 8, 10)
        )

        assert assigned.trips[0].has_vehicle
        assert not unassigned.trips[0].has_vehicle


class TestParseGps:
    def test_epoch_timestamp_decodes_to_utc(self) -> None:
        fix = _parse_gps(
            {
                "rowId": 0,
                "startTime": "2026-08-10T08:00:08",
                "timestamp": GPS_EPOCH,
                "longitude": -85.93753,
                "latitude": 39.17290,
            }
        )
        assert fix is not None
        assert fix.fix_time == datetime(2026, 8, 10, 12, 0, 8, tzinfo=timezone.utc)
        # xCoord/yCoord are not transposed: latitude is the northerly one.
        assert fix.latitude == 39.17290
        assert fix.longitude == -85.93753

    def test_no_fix_shapes_return_none(self) -> None:
        assert _parse_gps(None) is None
        assert _parse_gps({}) is None
        # null island
        assert _parse_gps({"latitude": 0, "longitude": 0, "timestamp": GPS_EPOCH}) is None
        # position without a timestamp cannot be aged, so it is unusable
        assert _parse_gps({"latitude": 39.1, "longitude": -85.9}) is None
        assert _parse_gps({"latitude": "nope", "longitude": -85.9, "timestamp": 1}) is None


class TestFreshness:
    def _fix(self) -> GpsFix:
        return GpsFix(
            latitude=39.17290,
            longitude=-85.93753,
            fix_time=datetime.fromtimestamp(GPS_EPOCH, tz=timezone.utc),
            raw={},
        )

    def test_fresh_within_five_minutes(self) -> None:
        fix = self._fix()
        assert fix.is_fresh(fix.fix_time + timedelta(seconds=187))
        assert fix.is_fresh(fix.fix_time + timedelta(seconds=300))

    def test_stale_beyond_five_minutes(self) -> None:
        fix = self._fix()
        assert not fix.is_fresh(fix.fix_time + timedelta(seconds=301))
        assert not fix.is_fresh(fix.fix_time + timedelta(minutes=10))


class TestJwtExp:
    def test_decodes_exp_claim_with_unpadded_base64(self) -> None:
        # The envelope's `expiration` is DateTime.MinValue, so `exp` is the truth.
        exp = 1786363208
        payload = base64.urlsafe_b64encode(
            json.dumps({"exp": exp}).encode()
        ).decode().rstrip("=")
        assert _jwt_exp(f"header.{payload}.signature") == datetime.fromtimestamp(
            exp, tz=timezone.utc
        )

    def test_malformed_tokens_return_none(self) -> None:
        assert _jwt_exp("not-a-jwt") is None
        assert _jwt_exp("") is None
