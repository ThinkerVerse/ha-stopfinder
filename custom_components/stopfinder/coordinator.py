"""Push/poll coordinator for Stopfinder.

Live position comes from GET /gps?groupName=... The coordinator:
  * logs in and caches the JWT (re-login on expiry; no refresh endpoint captured);
  * refreshes the day's roster and re-evaluates active trips on a slow tick;
  * while >=1 trip is active, polls /gps on a fast tick and pushes positions;
  * outside every trip window, does no GPS polling at all.

Availability is derived from fix freshness — the /gps payload has no status field.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    GpsFix,
    StopfinderApi,
    StopfinderAuthError,
    StudentSchedule,
    Tokens,
    Trip,
)
from .const import (
    CONF_DEVICE_ID,
    CONF_PASSWORD,
    CONF_USERNAME,
    DOMAIN,
    GPS_POLL_SECONDS,
    SCHEDULE_TICK_SECONDS,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class RiderState:
    """Everything the entities render for one student."""

    schedule: StudentSchedule
    active_trip: Trip | None = None
    fix: GpsFix | None = None

    @property
    def rider_id(self) -> int:
        return self.schedule.rider_id


class StopfinderCoordinator(DataUpdateCoordinator[dict[int, RiderState]]):
    """Coordinates login, schedule refresh, and /gps polling."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=None,  # we drive our own timers
        )
        self.entry = entry
        self.api = StopfinderApi(async_get_clientsession(hass))
        self._tokens: Tokens | None = None
        self._schedule_day: date | None = None
        self._unsub_schedule = None
        self._unsub_gps = None

    # -- lifecycle ------------------------------------------------------------

    async def async_setup(self) -> None:
        await self._ensure_token()
        await self.api.fetch_client_identity()
        await self._refresh_schedule()

        self._unsub_schedule = async_track_time_interval(
            self.hass, self._async_schedule_tick,
            timedelta(seconds=SCHEDULE_TICK_SECONDS),
        )
        self._unsub_gps = async_track_time_interval(
            self.hass, self._async_gps_tick,
            timedelta(seconds=GPS_POLL_SECONDS),
        )
        await self._async_schedule_tick(datetime.now(timezone.utc))

    async def async_shutdown(self) -> None:
        for unsub in (self._unsub_schedule, self._unsub_gps):
            if unsub:
                unsub()
        self._unsub_schedule = None
        self._unsub_gps = None

    # -- auth -----------------------------------------------------------------

    async def _ensure_token(self) -> None:
        if self._tokens is not None:
            exp = self._tokens.jwt_expires_at
            if exp and exp - datetime.now(timezone.utc) > timedelta(minutes=5):
                return
        data = self.entry.data
        if not self.api.base_uri:
            await self.api.discover(data[CONF_USERNAME])
        try:
            self._tokens = await self.api.login(
                data[CONF_USERNAME], data[CONF_PASSWORD], data[CONF_DEVICE_ID]
            )
        except StopfinderAuthError as err:
            raise UpdateFailed(f"Authentication failed: {err}") from err

    # -- schedule -------------------------------------------------------------

    async def _refresh_schedule(self) -> None:
        try:
            schedules = await self.api.fetch_students()
        except Exception as err:  # noqa: BLE001
            raise UpdateFailed(f"Roster fetch failed: {err}") from err

        new_data: dict[int, RiderState] = {}
        for sched in schedules:
            prev = (self.data or {}).get(sched.rider_id)
            new_data[sched.rider_id] = RiderState(
                schedule=sched,
                fix=prev.fix if prev else None,
            )
        self._schedule_day = date.today()
        self.async_set_updated_data(new_data)

    async def _async_schedule_tick(self, _now: datetime) -> None:
        await self._ensure_token()
        if self._schedule_day != date.today():
            await self._refresh_schedule()

        now = datetime.now(timezone.utc)
        for rider in (self.data or {}).values():
            if not rider.schedule.display_vehicle_on_map:
                rider.active_trip = None
                continue
            rider.active_trip = _active_trip(rider.schedule.trips, now)
        self.async_update_listeners()

    # -- gps poll -------------------------------------------------------------

    def _has_active_trip(self) -> bool:
        return any(r.active_trip for r in (self.data or {}).values())

    async def _async_gps_tick(self, _now: datetime) -> None:
        if not self._has_active_trip():
            return  # nothing running -> no polling

        changed = False
        for rider in (self.data or {}).values():
            trip = rider.active_trip
            if trip is None:
                continue
            group = rider.schedule.group_name(trip)
            try:
                fix = await self.api.fetch_gps(group)
            except StopfinderAuthError:
                await self._ensure_token()
                try:
                    fix = await self.api.fetch_gps(group)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("GPS retry failed for %s: %s", group, err)
                    continue
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("GPS poll failed for %s: %s", group, err)
                continue
            if fix is not None:
                rider.fix = fix
                changed = True
        if changed:
            self.async_update_listeners()


def _active_trip(trips: list[Trip], now: datetime) -> Trip | None:
    """Return the trip whose [start-before, finish+after] window contains now."""
    for trip in trips:
        window_start = trip.start_time - timedelta(minutes=trip.before_trip_min)
        window_end = trip.finish_time + timedelta(minutes=trip.after_trip_min)
        if window_start <= now <= window_end:
            return trip
    return None
