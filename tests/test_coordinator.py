"""Tests for derived GPS status and poll-group construction."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from sf.api import GpsFix, StudentSchedule, Trip
from sf.const import (
    GPS_NO_VEHICLE,
    GPS_NOT_AVAILABLE,
    GPS_SEARCHING,
    GPS_VALID,
)
from sf.coordinator import RiderState, StopfinderCoordinator, _active_trip

CLIENT_ID = 999
DATA_SOURCE_ID = 12

# Window: 07:00 -> 08:00 UTC, padded by 15 minutes at each end.
START = datetime(2026, 8, 10, 7, 0, tzinfo=timezone.utc)
FINISH = datetime(2026, 8, 10, 8, 0, tzinfo=timezone.utc)


def _trip(
    *,
    trip_id: int = 1,
    bus_number: str = "42",
    adjust_minutes: int = 0,
    start: datetime = START,
    finish: datetime = FINISH,
) -> Trip:
    return Trip(
        trip_id=trip_id,
        name=f"{bus_number} AM",
        bus_number=bus_number,
        vehicle_id=100,
        to_school=True,
        start_time=start,
        finish_time=finish,
        pickup_stop_id=1,
        dropoff_stop_id=2,
        pickup_stop_name="Elm St & 3rd",
        dropoff_stop_name="Example Elementary",
        before_trip_min=15,
        after_trip_min=15,
        adjust_minutes=adjust_minutes,
    )


def _rider(rider_id: int, trips: list[Trip]) -> RiderState:
    return RiderState(
        schedule=StudentSchedule(
            rider_id=rider_id,
            first_name="Test",
            last_name=f"Student {rider_id}",
            school="Example Elementary",
            client_id=CLIENT_ID,
            data_source_id=DATA_SOURCE_ID,
            display_vehicle_on_map=True,
            tz_offset_minutes=-240.0,
            trips=trips,
        )
    )


def _fix(at: datetime) -> GpsFix:
    return GpsFix(latitude=39.1, longitude=-85.9, fix_time=at, raw={})


class TestGpsStatus:
    def test_unknown_outside_any_window(self) -> None:
        rider = _rider(1, [_trip()])
        rider.active_trip = None
        assert rider.gps_status(START) is None

    def test_no_vehicle_assigned_short_circuits(self) -> None:
        trip = _trip(bus_number="")
        rider = _rider(1, [trip])
        rider.active_trip = trip
        # Reported even though a stale fix is lying around from an earlier trip.
        rider.fix = _fix(START - timedelta(hours=6))
        assert rider.gps_status(START) == GPS_NO_VEHICLE

    def test_valid_gps_when_the_fix_is_fresh(self) -> None:
        trip = _trip()
        rider = _rider(1, [trip])
        rider.active_trip = trip
        now = START + timedelta(minutes=20)
        rider.fix = _fix(now - timedelta(seconds=30))
        assert rider.gps_status(now) == GPS_VALID

    def test_not_available_when_the_fix_goes_stale(self) -> None:
        trip = _trip()
        rider = _rider(1, [trip])
        rider.active_trip = trip
        now = START + timedelta(minutes=20)
        rider.fix = _fix(now - timedelta(seconds=301))
        assert rider.gps_status(now) == GPS_NOT_AVAILABLE

    def test_searching_early_in_the_window_with_no_fix_yet(self) -> None:
        trip = _trip()
        rider = _rider(1, [trip])
        rider.active_trip = trip
        # Two minutes into the window, still waiting on the first report.
        assert rider.gps_status(trip.window_start + timedelta(minutes=2)) == GPS_SEARCHING

    def test_gives_up_to_not_available_after_five_minutes(self) -> None:
        trip = _trip()
        rider = _rider(1, [trip])
        rider.active_trip = trip
        assert (
            rider.gps_status(trip.window_start + timedelta(minutes=5))
            == GPS_NOT_AVAILABLE
        )


class TestActiveTrip:
    def test_picks_the_trip_whose_window_contains_now(self) -> None:
        am = _trip(trip_id=1)
        pm = _trip(
            trip_id=2,
            start=datetime(2026, 8, 10, 19, 0, tzinfo=timezone.utc),
            finish=datetime(2026, 8, 10, 20, 0, tzinfo=timezone.utc),
        )
        trips = [am, pm]

        assert _active_trip(trips, START + timedelta(minutes=30)) is am
        assert _active_trip(trips, datetime(2026, 8, 10, 19, 30, tzinfo=timezone.utc)) is pm
        assert _active_trip(trips, datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)) is None

    def test_adjust_minutes_is_honoured(self) -> None:
        # 08:20 UTC is 5 minutes past the padded end of an unshifted window.
        moment = datetime(2026, 8, 10, 8, 20, tzinfo=timezone.utc)
        assert _active_trip([_trip()], moment) is None
        assert _active_trip([_trip(adjust_minutes=30)], moment) is not None


class TestPollGroups:
    def _coordinator(self, riders: dict[int, RiderState]) -> StopfinderCoordinator:
        coordinator = StopfinderCoordinator(hass=None, entry=None)
        coordinator.data = riders
        return coordinator

    def test_siblings_on_one_bus_share_a_single_request(self) -> None:
        trip = _trip(bus_number="42")
        first, second = _rider(1, [trip]), _rider(2, [trip])
        first.active_trip = second.active_trip = trip

        groups = self._coordinator({1: first, 2: second})._groups_to_poll()

        assert list(groups) == [f"{CLIENT_ID}_{DATA_SOURCE_ID}_42"]
        assert groups[f"{CLIENT_ID}_{DATA_SOURCE_ID}_42"] == [first, second]

    def test_different_buses_get_their_own_groups(self) -> None:
        first_trip, second_trip = _trip(bus_number="42"), _trip(bus_number="43")
        first, second = _rider(1, [first_trip]), _rider(2, [second_trip])
        first.active_trip, second.active_trip = first_trip, second_trip

        groups = self._coordinator({1: first, 2: second})._groups_to_poll()

        assert sorted(groups) == [
            f"{CLIENT_ID}_{DATA_SOURCE_ID}_42",
            f"{CLIENT_ID}_{DATA_SOURCE_ID}_43",
        ]

    def test_riders_without_an_active_trip_or_vehicle_are_skipped(self) -> None:
        idle = _rider(1, [_trip()])
        idle.active_trip = None

        unassigned_trip = _trip(bus_number="")
        unassigned = _rider(2, [unassigned_trip])
        unassigned.active_trip = unassigned_trip

        assert self._coordinator({1: idle, 2: unassigned})._groups_to_poll() == {}
