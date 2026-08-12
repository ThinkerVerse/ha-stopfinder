"""Poll coordinator for Stopfinder.

Two timers, and three windows that decide what either may do:

  * the schedule tick runs always. It computes which trips are running, which is
    purely local. Two things make it reach the network: the roster refresh at day
    rollover (unavoidable, since the windows are derived from it) and district
    announcements, which have a window of their own;
  * the in-window timer is armed only while a trip window is open, and carries
    /gps every tick plus geo-alert notifications on their own slower clock.

The windows:

  * trip window   [start - beforeTrip, finish + afterTrip]   -> /gps, geo alerts
  * announcement  [first start - lead, last finish + trail]  -> announcements
  * neither       -> no requests at all

On a day with no trips nothing is requested. On a school day it is one roster
fetch, announcements across the wider bracket, and /gps plus geo alerts only
while a bus is actually running.

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
    Announcement,
    GeoAlert,
    GpsFix,
    StopfinderApi,
    StopfinderAuthError,
    StudentSchedule,
    Tokens,
    Trip,
)
from .const import (
    ANNOUNCEMENT_LEAD_HOURS,
    ANNOUNCEMENT_POLL_MINUTES,
    ANNOUNCEMENT_TRAIL_HOURS,
    CONF_ANNOUNCEMENT_LEAD_HOURS,
    CONF_ANNOUNCEMENT_POLL_MINUTES,
    CONF_ANNOUNCEMENT_TRAIL_HOURS,
    CONF_CLIENT_KEYS,
    CONF_GEO_ALERT_POLL_SECONDS,
    CONF_DEVICE_ID,
    CONF_GPS_POLL_SECONDS,
    CONF_PASSWORD,
    CONF_SCHEDULE_TICK_SECONDS,
    CONF_REFRESH_TOKEN,
    CONF_SUBSCRIBER_ID,
    CONF_USERNAME,
    DEFAULT_LANGUAGE,
    DOMAIN,
    EVENT_ANNOUNCEMENT,
    EVENT_GEO_ALERT,
    GEO_ALERT_POLL_SECONDS,
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
    geo_alert: GeoAlert | None = None
    district: str = ""

    @property
    def rider_id(self) -> int:
        return self.schedule.rider_id

    def next_pickup(self, now: datetime) -> Trip | None:
        """The next trip that has yet to reach this student's stop today.

        Once the last pickup of the day has passed this is None: the roster we
        hold covers today only, so there is no honest answer for tomorrow.
        """
        return _soonest(self.schedule.trips, "adjusted_pickup_time", now)

    def next_dropoff(self, now: datetime) -> Trip | None:
        """The next trip that has yet to drop this student off today."""
        return _soonest(self.schedule.trips, "adjusted_dropoff_time", now)

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
        self._schedule_interval: timedelta | None = None
        self._unsub_gps = None
        self._polling_interval: timedelta | None = None
        self._auth_failed = False
        self._seen_geo_alerts: set[str] = set()
        self._geo_alerts_primed = False
        self._geo_alerts_polled_at: datetime | None = None
        self.announcement: Announcement | None = None
        self._seen_announcements: set[str] = set()
        self._announcements_primed = False
        self._announcements_polled_at: datetime | None = None

    # -- lifecycle ------------------------------------------------------------

    async def async_setup(self) -> None:
        await self._ensure_token()
        await self.api.fetch_client_identity()
        await self._refresh_schedule()

        self._start_schedule_timer()
        # The GPS timer is not started here: the schedule tick owns it, and only
        # runs it while a trip window is open.
        await self._async_schedule_tick(datetime.now(timezone.utc))

    async def async_shutdown(self) -> None:
        self._stop_schedule_timer()
        self._stop_gps_polling()

    def _start_schedule_timer(self) -> None:
        if self._unsub_schedule is not None:
            return
        self._schedule_interval = self.schedule_tick_interval
        self._unsub_schedule = async_track_time_interval(
            self.hass, self._async_schedule_tick, self._schedule_interval
        )

    def _stop_schedule_timer(self) -> None:
        if self._unsub_schedule is not None:
            self._unsub_schedule()
            self._unsub_schedule = None
        self._schedule_interval = None

    # -- poll cadence ---------------------------------------------------------

    @property
    def district(self) -> str:
        """The district the API is scoped to, tidied for display.

        There is no friendly district name to be had: the only `clientName` in
        the app comes from message threads, not from any endpoint we call. So
        this is the district key ("bartholomew") cased for reading
        ("Bartholomew"). Display only — `x-client-keys` still goes out lowercase
        from api.client_keys.
        """
        raw = self.api.client_id or self.entry.data.get(CONF_CLIENT_KEYS, "")
        return _title_case(raw)

    @property
    def _geo_alert_poll_interval(self) -> timedelta:
        seconds = self.entry.options.get(
            CONF_GEO_ALERT_POLL_SECONDS, GEO_ALERT_POLL_SECONDS
        )
        return timedelta(seconds=int(seconds))

    @property
    def schedule_tick_interval(self) -> timedelta:
        """How often window state is re-evaluated (local work, not a request)."""
        seconds = self.entry.options.get(
            CONF_SCHEDULE_TICK_SECONDS, SCHEDULE_TICK_SECONDS
        )
        return timedelta(seconds=int(seconds))

    @property
    def announcement_window(self) -> tuple[datetime, datetime] | None:
        """The day's trips bracketed by the configured lead and trail.

        Wider than the trip windows on purpose: a "bus running late" notice goes
        out well before the bus is due, so confining announcements to the trip
        windows would surface it only once it had stopped being useful. Anchored
        on the route's own start and finish, so a 07:21 route with a 3h lead
        starts polling at 04:21.

        None when today has no trips at all — a weekend or a snow day stays
        completely silent.
        """
        starts: list[datetime] = []
        ends: list[datetime] = []
        for rider in (self.data or {}).values():
            for trip in rider.schedule.trips:
                starts.append(trip.adjusted_start_time)
                ends.append(trip.adjusted_finish_time)
        if not starts:
            return None
        lead = timedelta(
            hours=int(
                self.entry.options.get(
                    CONF_ANNOUNCEMENT_LEAD_HOURS, ANNOUNCEMENT_LEAD_HOURS
                )
            )
        )
        trail = timedelta(
            hours=int(
                self.entry.options.get(
                    CONF_ANNOUNCEMENT_TRAIL_HOURS, ANNOUNCEMENT_TRAIL_HOURS
                )
            )
        )
        return min(starts) - lead, max(ends) + trail

    def _in_announcement_window(self, now: datetime) -> bool:
        window = self.announcement_window
        if window is None:
            return False
        start, end = window
        return start <= now <= end

    @property
    def _announcement_poll_interval(self) -> timedelta:
        minutes = self.entry.options.get(
            CONF_ANNOUNCEMENT_POLL_MINUTES, ANNOUNCEMENT_POLL_MINUTES
        )
        return timedelta(minutes=int(minutes))

    @property
    def _language(self) -> str:
        """Language for geo-alert text; the app sends the subscriber's own."""
        return getattr(self.hass.config, "language", None) or DEFAULT_LANGUAGE

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
        if self._schedule_interval != self.schedule_tick_interval:
            self._stop_schedule_timer()
            self._start_schedule_timer()

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
                geo_alert=prev.geo_alert if prev else None,
                district=self.district,
            )
        self._schedule_day = date.today()
        self.async_set_updated_data(new_data)

    async def _async_schedule_tick(self, now: datetime | None = None) -> None:
        """Decide whether a window is open. Makes no network call of its own.

        The roster is the one exception, and only at day rollover: the windows
        themselves are derived from it, so it cannot be deferred until a window
        is already open. Everything else rides the in-window timer.
        """
        if self._auth_failed:
            return
        # Home Assistant passes the fire time; honour it rather than re-reading
        # the clock, so window transitions and the tick agree on "now".
        now = now or datetime.now(timezone.utc)

        if self._schedule_day != date.today():
            try:
                await self._ensure_token()
                await self._refresh_schedule()
            except ConfigEntryAuthFailed:
                self._handle_auth_failure()
                return
            except Exception as err:  # noqa: BLE001 - retry on the next tick
                _LOGGER.debug("Roster refresh failed: %s", err)
                return

        for rider in (self.data or {}).values():
            if not rider.schedule.display_vehicle_on_map:
                rider.active_trip = None
                continue
            rider.active_trip = _active_trip(rider.schedule.trips, now)
        self.async_update_listeners()

        if self._groups_to_poll():
            if self._start_gps_polling():
                # Window just opened. Clear the in-window clock so the first tick
                # fetches immediately instead of waiting out an interval — the
                # app does the same with startWith(0).
                self._geo_alerts_polled_at = None
                await self._async_gps_tick(now)
        else:
            self._stop_gps_polling()

        # Announcements answer to their own, wider window, so they ride this
        # always-running tick rather than the in-window timer.
        if self._in_announcement_window(now) and self._announcements_due(now):
            try:
                await self._ensure_token()
                await self._async_poll_announcements(now)
            except ConfigEntryAuthFailed:
                self._handle_auth_failure()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Announcement poll failed: %s", err)

    # -- geo alerts -----------------------------------------------------------

    def _geo_alert_requests(self) -> list[dict[str, int]]:
        """Build the app's payload: one entry per rider/trip for today.

        The app skips riders with no dataSourceId or no trips, and sends every
        trip rather than just the running one — so an alert that fires late in a
        window is still attributed to the right trip.
        """
        subscriber_id = self.entry.data.get(CONF_SUBSCRIBER_ID)
        if subscriber_id is None:
            return []
        requests: list[dict[str, int]] = []
        for rider in (self.data or {}).values():
            schedule = rider.schedule
            if not schedule.data_source_id or not schedule.trips:
                continue
            for trip in schedule.trips:
                requests.append(
                    {
                        "riderId": schedule.rider_id,
                        "subscriberId": subscriber_id,
                        "tripId": trip.trip_id,
                        "dataSourceId": schedule.data_source_id,
                    }
                )
        return requests

    def _geo_alerts_due(self, now: datetime) -> bool:
        if self._geo_alerts_polled_at is None:
            return True
        return now - self._geo_alerts_polled_at >= self._geo_alert_poll_interval

    async def _async_poll_geo_alerts(self, now: datetime) -> None:
        """Fetch geo alerts and raise an event for any we have not seen.

        The endpoint returns the latest alert per rider/trip on every call, so
        without deduping on the alert id an automation would re-fire each minute.
        The first poll only primes that set: a restart should not replay an alert
        that fired before Home Assistant came up.
        """
        requests = self._geo_alert_requests()
        if not requests:
            return

        subscriber_id = self.entry.data[CONF_SUBSCRIBER_ID]
        try:
            alerts = await self.api.fetch_geo_alerts(
                subscriber_id, requests, self._language
            )
        except StopfinderAuthError:
            await self._ensure_token()
            alerts = await self.api.fetch_geo_alerts(
                subscriber_id, requests, self._language
            )
        self._geo_alerts_polled_at = now

        priming = not self._geo_alerts_primed
        self._geo_alerts_primed = True

        changed = False
        for alert in alerts:
            rider = (self.data or {}).get(alert.rider_id)
            if rider is None:
                continue
            if _is_newer(alert, rider.geo_alert):
                rider.geo_alert = alert
                changed = True
            if alert.alert_id in self._seen_geo_alerts:
                continue
            self._seen_geo_alerts.add(alert.alert_id)
            if not priming:
                self._fire_geo_alert_event(rider, alert)

        if changed:
            self.async_update_listeners()

    def _fire_geo_alert_event(self, rider: RiderState, alert: GeoAlert) -> None:
        self.hass.bus.async_fire(
            EVENT_GEO_ALERT,
            {
                "entry_id": self.entry.entry_id,
                "rider_id": alert.rider_id,
                "student": f"{rider.schedule.first_name} "
                f"{rider.schedule.last_name}".strip(),
                "trip_id": alert.trip_id,
                "zone": alert.zone_name,
                "subject": alert.subject,
                "message": alert.body,
                "alert_type": alert.alert_type,
                "sent_on": alert.sent_on.isoformat() if alert.sent_on else None,
                "alert_id": alert.alert_id,
            },
        )

    # -- announcements --------------------------------------------------------

    def _announcements_due(self, now: datetime) -> bool:
        """Announcements have their own slow clock, independent of trips.

        A "bus running late" notice is worth having before the window opens, so
        this cannot be gated on an active trip the way geo alerts are.
        """
        if self._announcements_polled_at is None:
            return True
        return now - self._announcements_polled_at >= self._announcement_poll_interval

    async def _async_poll_announcements(self, now: datetime) -> None:
        """Refresh announcements, raising an event for any not seen before.

        The endpoint returns the subscriber's whole history — an announcement
        from last school year is a normal response — so the newest record is not
        necessarily news. New ids raise the event; the first poll only primes.
        """
        try:
            announcements = await self.api.fetch_announcements()
        except StopfinderAuthError:
            await self._ensure_token()
            announcements = await self.api.fetch_announcements()
        self._announcements_polled_at = now

        priming = not self._announcements_primed
        self._announcements_primed = True

        latest = announcements[0] if announcements else None
        if latest is not None and (
            self.announcement is None
            or latest.announcement_id != self.announcement.announcement_id
        ):
            self.announcement = latest
            self.async_update_listeners()

        for announcement in announcements:
            if announcement.announcement_id in self._seen_announcements:
                continue
            self._seen_announcements.add(announcement.announcement_id)
            if not priming:
                self._fire_announcement_event(announcement)

    def _fire_announcement_event(self, announcement: Announcement) -> None:
        self.hass.bus.async_fire(
            EVENT_ANNOUNCEMENT,
            {
                "entry_id": self.entry.entry_id,
                "announcement_id": announcement.announcement_id,
                "subject": announcement.subject,
                "message": announcement.body,
                "sent_on": (
                    announcement.sent_on.isoformat() if announcement.sent_on else None
                ),
                "sent_by": announcement.sent_by_name,
                "read": announcement.read,
                "archived": announcement.archived,
            },
        )

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

    async def _async_gps_tick(self, now: datetime | None = None) -> None:
        """The in-window clock: every recurring network call rides this timer.

        It exists only while a trip window is open, which is what keeps the
        integration silent the rest of the day. Geo alerts and announcements are
        checked here rather than on the schedule tick for the same reason — and
        because the schedule tick is too slow to hit a sub-minute cadence.
        """
        if self._auth_failed:
            return
        now = now or datetime.now(timezone.utc)
        groups = self._groups_to_poll()
        if not groups:
            return  # nothing running -> no polling

        try:
            await self._ensure_token()
        except ConfigEntryAuthFailed:
            self._handle_auth_failure()
            return

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

        if self._geo_alerts_due(now):
            try:
                await self._async_poll_geo_alerts(now)
            except ConfigEntryAuthFailed:
                self._handle_auth_failure()
                return
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Geo alert poll failed: %s", err)

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


def _title_case(value: str) -> str:
    """Case a district key for display.

    Only touches keys that arrive entirely lowercase, so a district whose id is
    an acronym ("BCSC") is left as it is rather than mangled into "Bcsc".
    Comma-separated keys — what a multi-district parent gets — are cased
    individually.
    """
    if not value:
        return ""
    parts = [part.strip() for part in value.split(",")]
    return ", ".join(part.title() if part.islower() else part for part in parts)


def _soonest(trips: list[Trip], attr: str, now: datetime) -> Trip | None:
    """The trip with the earliest still-future value of `attr`."""
    upcoming = [
        trip for trip in trips
        if getattr(trip, attr) is not None and getattr(trip, attr) >= now
    ]
    if not upcoming:
        return None
    return min(upcoming, key=lambda trip: getattr(trip, attr))


def _is_newer(alert: GeoAlert, current: GeoAlert | None) -> bool:
    """Whether `alert` should replace what a rider is already showing."""
    if current is None:
        return True
    if alert.alert_id == current.alert_id:
        return False
    if alert.sent_on and current.sent_on:
        return alert.sent_on >= current.sent_on
    return True


def _active_trip(trips: list[Trip], now: datetime) -> Trip | None:
    """Return the trip whose window contains now.

    The window is the app's isTripRunning(): the trip's own adjustMinutes shifts
    both ends, then the student's beforeTrip/afterTrip pad them.
    """
    for trip in trips:
        if trip.is_running(now):
            return trip
    return None
