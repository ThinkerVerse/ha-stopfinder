"""Poll coordinator for Stopfinder.

Live position comes from GET /gps?groupName=... The coordinator:
  * logs in, then keeps the JWT alive with the refresh grant (falling back to a
    full re-login only if the refresh token is rejected);
  * refreshes the day's roster and re-evaluates active trips on a slow tick;
  * while >=1 trip is active, polls /gps on a fast tick and pushes positions;
  * outside every trip window, does no GPS polling at all.

Availability is derived from fix freshness — the /gps payload has no status
field, and the app derives its own `gpsStatus` the same way.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
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
    CONF_GPS_POLL_SECONDS,
    CONF_PASSWORD,
    CONF_REFRESH_TOKEN,
    CONF_USERNAME,
    DOMAIN,
    GPS_NO_VEHICLE,
    GPS_NOT_AVAILABLE,
    GPS_POLL_SECONDS,
    GPS_SEARCHING,
    GPS_STALE_AFTER_SECONDS,
    GPS_VALID,
    SCHEDULE_TICK_SECONDS,
    TOKEN_REFRESH_MARGIN_SECONDS,
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

    def gps_status(self, now: datetime) -> str | None:
        """Derive the app's `gpsStatus` for this rider.

        Mirrors the app's own vocabulary: it checks for an assigned vehicle
        first, shows Searching while it waits, and gives up to NotAvailable once
        nothing fresh has arrived for the staleness window. None means "outside
        any trip window", which surfaces as unknown.
        """
        trip = self.active_trip
        if trip is None:
            return None
        if not trip.has_vehicle:
            return GPS_NO_VEHICLE
        if self.fix and self.fix.is_fresh(now):
            return GPS_VALID
        if self.fix:
            return GPS_NOT_AVAILABLE
        # Never had a fix for this window. The app shows Searching, then drops to
        # NotAvailable after the same 5 minutes it uses for staleness.
        if now - trip.window_start >= timedelta(seconds=GPS_STALE_AFTER_SECONDS):
            return GPS_NOT_AVAILABLE
        return GPS_SEARCHING


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
        self._polling_interval: timedelta | None = None
        self._auth_failed = False

    # -- lifecycle ------------------------------------------------------------

    async def async_setup(self) -> None:
        await self._ensure_token()
        await self.api.fetch_client_identity()
        await self._refresh_schedule()

        self._unsub_schedule = async_track_time_interval(
            self.hass, self._async_schedule_tick,
            timedelta(seconds=SCHEDULE_TICK_SECONDS),
        )
        # The GPS timer is not started here: the schedule tick owns it, and only
        # runs it while a trip window is open.
        await self._async_schedule_tick(datetime.now(timezone.utc))

    async def async_shutdown(self) -> None:
        if self._unsub_schedule:
            self._unsub_schedule()
        self._unsub_schedule = None
        self._stop_gps_polling()

    # -- poll cadence ---------------------------------------------------------

    @property
    def gps_poll_interval(self) -> timedelta:
        """Configured /gps cadence for this entry."""
        seconds = self.entry.options.get(CONF_GPS_POLL_SECONDS, GPS_POLL_SECONDS)
        return timedelta(seconds=int(seconds))

    def _start_gps_polling(self) -> bool:
        """Arm the GPS timer. Returns True if it was not already running."""
        if self._unsub_gps is not None:
            return False
        self._polling_interval = self.gps_poll_interval
        self._unsub_gps = async_track_time_interval(
            self.hass, self._async_gps_tick, self._polling_interval
        )
        return True

    def _stop_gps_polling(self) -> None:
        """Disarm the GPS timer so nothing is polled outside a trip window."""
        if self._unsub_gps is not None:
            self._unsub_gps()
            self._unsub_gps = None
        self._polling_interval = None

    def async_apply_options(self) -> None:
        """Re-arm at a new cadence when the options change.

        Deliberately not a config-entry reload: the coordinator writes the
        rotated refresh token back to the entry, and reloading on every entry
        update would restart the integration each time a token is renewed.
        """
        if self._unsub_gps is None:
            return  # not polling; the next window will pick up the new value
        if self._polling_interval == self.gps_poll_interval:
            return
        self._stop_gps_polling()
        self._start_gps_polling()

    # -- auth -----------------------------------------------------------------

    async def _ensure_token(self) -> None:
        """Make sure a usable JWT is on the API client.

        Prefers the refresh grant, as the app does, and only re-sends the
        password if the refresh token is rejected (or we never had one).
        """
        if self._tokens is not None:
            exp = self._tokens.jwt_expires_at
            margin = timedelta(seconds=TOKEN_REFRESH_MARGIN_SECONDS)
            if exp and exp - datetime.now(timezone.utc) > margin:
                return

        data = self.entry.data
        if not self.api.base_uri:
            await self.api.discover(data[CONF_USERNAME])

        refresh_token = (
            self._tokens.refresh_token if self._tokens else None
        ) or data.get(CONF_REFRESH_TOKEN)

        if refresh_token:
            try:
                self._store_tokens(
                    await self.api.refresh(
                        data[CONF_USERNAME], refresh_token, data[CONF_DEVICE_ID]
                    )
                )
                return
            except StopfinderAuthError as err:
                _LOGGER.debug("Refresh token rejected, re-logging in: %s", err)
            except Exception as err:  # noqa: BLE001 - fall back to a full login
                _LOGGER.debug("Token refresh failed, re-logging in: %s", err)

        try:
            self._store_tokens(
                await self.api.login(
                    data[CONF_USERNAME], data[CONF_PASSWORD], data[CONF_DEVICE_ID]
                )
            )
        except StopfinderAuthError as err:
            # Surfaces in the UI as "reconfigure this integration".
            raise ConfigEntryAuthFailed(f"Authentication failed: {err}") from err

    def _handle_auth_failure(self) -> None:
        """Ask the user to re-authenticate and stop polling until they do.

        ConfigEntryAuthFailed only starts a reauth flow when it escapes setup or
        a coordinator refresh. Raised from one of our own timer callbacks it
        would just be logged, so the flow has to be started by hand — and polling
        stopped, or we would retry a known-bad credential every tick.
        """
        if self._auth_failed:
            return
        self._auth_failed = True
        self._stop_gps_polling()
        _LOGGER.warning(
            "Stopfinder rejected the stored credentials; asking for re-authentication"
        )
        self.entry.async_start_reauth(self.hass)

    def _store_tokens(self, tokens: Tokens) -> None:
        """Keep the tokens, persisting the refresh token if it rotated.

        The refresh token rotates on every use, so the newest one has to outlive
        a restart or the next refresh fails and we fall back to the password.
        """
        self._tokens = tokens
        if not tokens.refresh_token:
            return
        if self.entry.data.get(CONF_REFRESH_TOKEN) == tokens.refresh_token:
            return
        self.hass.config_entries.async_update_entry(
            self.entry,
            data={**self.entry.data, CONF_REFRESH_TOKEN: tokens.refresh_token},
        )

    # -- schedule -------------------------------------------------------------

    async def _refresh_schedule(self) -> None:
        try:
            try:
                schedules = await self.api.fetch_students()
            except StopfinderAuthError:
                # Token died between ticks: renew once, then retry.
                await self._ensure_token()
                schedules = await self.api.fetch_students()
        except ConfigEntryAuthFailed:
            raise
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
        if self._auth_failed:
            return
        try:
            await self._ensure_token()
            if self._schedule_day != date.today():
                await self._refresh_schedule()
        except ConfigEntryAuthFailed:
            self._handle_auth_failure()
            return

        now = datetime.now(timezone.utc)
        for rider in (self.data or {}).values():
            if not rider.schedule.display_vehicle_on_map:
                rider.active_trip = None
                continue
            rider.active_trip = _active_trip(rider.schedule.trips, now)
        self.async_update_listeners()

        # Open or close the GPS timer to match the window state. Starting it also
        # polls straight away rather than waiting out a first interval — the app
        # does the same with startWith(0).
        if self._groups_to_poll():
            if self._start_gps_polling():
                await self._async_gps_tick(now)
        else:
            self._stop_gps_polling()

    # -- gps poll -------------------------------------------------------------

    def _groups_to_poll(self) -> dict[str, list[RiderState]]:
        """Map each group name that needs polling to the riders it serves.

        Siblings on the same bus share one group name, and so share one request —
        the app caches by endpoint for the same reason.
        """
        groups: dict[str, list[RiderState]] = {}
        for rider in (self.data or {}).values():
            trip = rider.active_trip
            if trip is None or not trip.has_vehicle:
                continue  # no vehicle assigned -> nothing to ask for
            groups.setdefault(rider.schedule.group_name(trip), []).append(rider)
        return groups

    async def _async_gps_tick(self, _now: datetime) -> None:
        if self._auth_failed:
            return
        groups = self._groups_to_poll()
        if not groups:
            return  # nothing running -> no polling

        changed = False
        for group, riders in groups.items():
            try:
                fix = await self._fetch_gps(group)
            except ConfigEntryAuthFailed:
                self._handle_auth_failure()
                return
            if fix is not None:
                for rider in riders:
                    rider.fix = fix
                changed = True
        if changed:
            self.async_update_listeners()

    async def _fetch_gps(self, group: str) -> GpsFix | None:
        """Poll one group, refreshing the token once if it has expired."""
        try:
            return await self.api.fetch_gps(group)
        except StopfinderAuthError:
            try:
                await self._ensure_token()
                return await self.api.fetch_gps(group)
            except ConfigEntryAuthFailed:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("GPS retry failed for %s: %s", group, err)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("GPS poll failed for %s: %s", group, err)
        return None


def _active_trip(trips: list[Trip], now: datetime) -> Trip | None:
    """Return the trip whose window contains now.

    The window is the app's isTripRunning(): the trip's own adjustMinutes shifts
    both ends, then the student's beforeTrip/afterTrip pad them.
    """
    for trip in trips:
        if trip.is_running(now):
            return trip
    return None
