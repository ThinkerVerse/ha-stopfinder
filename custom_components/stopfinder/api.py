"""Async REST client for the Stopfinder API.

Implements the bootstrap sequence observed in the app:

    1. discovery      GET  mytransfinder.com/$xcom/getStopfinder.asp?email=...
                      -> plain-text base URL for the district
    2. login          POST {base}/tokens
                      -> {token (JWT), refreshToken, opaqueToken}
    3. apiversions    GET  {base}/systems/apiversions   (token header)
                      -> [{sfClientId, clientId, ...}]  (district identity)
    4. subscriber     GET  {base}/action/subscribers/current
    5. students       GET  {base}/students?dateStart=..&dateEnd=..

Auth is a custom `token:` header carrying the raw JWT (NOT `Authorization: Bearer`).
The opaque token is only used for the SignalR vehicle hub, so it is returned to
the caller but never attached to REST calls here.
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

import aiohttp
from yarl import URL

from .const import (
    APP_VERSION,
    AUTH_FAILURE_STATUSES,
    DEFAULT_LANGUAGE,
    DISCOVERY_URL,
    PATH_ANNOUNCEMENTS,
    PATH_APIVERSIONS,
    GPS_STALE_AFTER_SECONDS,
    PATH_GEO_ALERTS,
    PATH_GPS,
    PATH_STUDENTS,
    PATH_SUBSCRIBER,
    PATH_TOKENS,
    RF_API_VERSION,
    USER_AGENT,
)

_LOGGER = logging.getLogger(__name__)


class StopfinderError(Exception):
    """Base error."""


class StopfinderAuthError(StopfinderError):
    """Raised when credentials are rejected."""


@dataclass
class Tokens:
    """Result of a successful login."""

    token: str          # JWT, used in the `token:` header for REST
    opaque_token: str    # used for the SignalR vehicle hub access_token
    refresh_token: str | None = None

    @property
    def jwt_expires_at(self) -> datetime | None:
        """Decode the JWT `exp` claim.

        The envelope `expiration` field is DateTime.MinValue (unpopulated), so the
        real expiry lives in the JWT payload.
        """
        return _jwt_exp(self.token)


@dataclass
class GpsFix:
    """A single live position from GET /gps.

    The payload has no status/speed/heading fields — only a position and a
    Unix-epoch `timestamp`. Availability is derived from freshness.
    """

    latitude: float
    longitude: float
    fix_time: datetime  # from `timestamp` (epoch seconds, UTC)
    raw: dict[str, Any]

    def is_fresh(self, now: datetime | None = None, max_age: int = GPS_STALE_AFTER_SECONDS) -> bool:
        now = now or datetime.now(timezone.utc)
        return (now - self.fix_time).total_seconds() <= max_age


@dataclass
class GeoAlert:
    """One geo-alert notification: the bus reaching a zone the user drew.

    Returned by POST /GeoAlertNotifications/{subscriberId}, which reports the
    most recent notification per (rider, trip) — so the same alert comes back on
    every poll until a newer one replaces it. Callers dedupe on `alert_id`.
    """

    alert_id: str
    rider_id: int
    trip_id: int
    zone_name: str      # `name`, e.g. the geofence the user drew
    subject: str        # the notification title (the app puts the student here)
    body: str           # the notification text
    sent_on: datetime | None
    created_at: datetime | None
    alert_type: bool | None
    raw: dict[str, Any]


@dataclass
class Announcement:
    """A district-wide notice from GET /announcementssent.

    These are the "bus 233 running 20 minutes late" messages. The endpoint
    returns the subscriber's whole history — an announcement from last school
    year is a perfectly normal response — so `sent_on` is what separates a live
    notice from an archived one, not the mere presence of a record.
    """

    announcement_id: str
    subject: str
    body: str
    name: str
    sent_on: datetime | None
    opened_on: datetime | None
    sent_by_name: str
    read: bool
    archived: bool
    raw: dict[str, Any]


@dataclass
class Trip:
    """One AM or PM trip for a student on a given day."""

    trip_id: int
    name: str
    bus_number: str
    vehicle_id: int
    to_school: bool
    start_time: datetime
    finish_time: datetime
    pickup_stop_id: int
    dropoff_stop_id: int
    pickup_stop_name: str
    dropoff_stop_name: str
    before_trip_min: int
    after_trip_min: int
    # When the bus reaches *this student's* stop, as opposed to start/finish
    # which bracket the whole route. Naive district-local like the rest: the app
    # runs all four through the same conversion.
    pickup_time: datetime | None = None
    dropoff_time: datetime | None = None
    # Per-trip shift the app's isTripRunning() applies to both ends of the
    # window *before* the student's before/after padding. A district that sets it
    # (it can even push a stop across midnight, which the app renders as
    # "(+1)"/"(-1)") would otherwise leave us with a window skewed by that many
    # minutes.
    adjust_minutes: int = 0

    @property
    def window_start(self) -> datetime:
        """Start of the polling window."""
        return (
            self.start_time
            + timedelta(minutes=self.adjust_minutes)
            - timedelta(minutes=self.before_trip_min)
        )

    @property
    def window_end(self) -> datetime:
        """End of the polling window."""
        return (
            self.finish_time
            + timedelta(minutes=self.adjust_minutes)
            + timedelta(minutes=self.after_trip_min)
        )

    def is_running(self, now: datetime) -> bool:
        """Whether `now` falls inside this trip's window (inclusive, as the app)."""
        return self.window_start <= now <= self.window_end

    @property
    def adjusted_pickup_time(self) -> datetime | None:
        """Pickup time with adjustMinutes applied, as the app's display does."""
        if self.pickup_time is None:
            return None
        return self.pickup_time + timedelta(minutes=self.adjust_minutes)

    @property
    def adjusted_dropoff_time(self) -> datetime | None:
        """Dropoff time with adjustMinutes applied."""
        if self.dropoff_time is None:
            return None
        return self.dropoff_time + timedelta(minutes=self.adjust_minutes)

    @property
    def has_vehicle(self) -> bool:
        """A trip with no bus assigned can't be tracked.

        The app checks this before it builds a request at all, and reports
        NoVehicleAssigned rather than hitting the network.
        """
        return bool(self.bus_number)


@dataclass
class StudentSchedule:
    """A student plus the day's trips and the identifiers the vehicle hub needs."""

    rider_id: int
    first_name: str
    last_name: str
    school: str
    client_id: int
    data_source_id: int
    display_vehicle_on_map: bool
    tz_offset_minutes: float
    grade: str = ""
    trips: list[Trip] = field(default_factory=list)

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def home_stop(self) -> str:
        """The stop at the student's end of the route.

        It is the pickup on the way to school and the dropoff on the way back,
        so take whichever the day's trips provide. Unlike the school end, this
        is the one that stays put across the year.
        """
        for trip in self.trips:
            name = trip.pickup_stop_name if trip.to_school else trip.dropoff_stop_name
            if name:
                return name
        return ""

    def group_name(self, trip: Trip) -> str:
        """Vehicle-hub group: {clientId}_{dataSourceId}_{busNumber}."""
        return f"{self.client_id}_{self.data_source_id}_{trip.bus_number}"


class StopfinderApi:
    """Thin async wrapper over the Stopfinder REST endpoints."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        self.base_uri: str | None = None
        self.client_keys: str = ""
        # The clientId as the API spelled it, before lowercasing for the header.
        self.client_id: str = ""
        self.sf_client_id: int | None = None
        self._token: str | None = None

    # -- headers --------------------------------------------------------------

    def _base_headers(self) -> dict[str, str]:
        h = {
            "accept": "application/json, text/plain, */*",
            "x-stopfinderapp-version": APP_VERSION,
            "user-agent": USER_AGENT,
        }
        if self.client_keys:
            h["x-client-keys"] = self.client_keys
        return h

    def _auth_headers(self) -> dict[str, str]:
        h = self._base_headers()
        if self._token:
            h["token"] = self._token  # custom header, not Bearer
        return h

    @staticmethod
    def _raise_for_auth(resp: aiohttp.ClientResponse) -> None:
        """Reject the statuses the app's interceptor refreshes on.

        203 has to be checked by hand: it is a 2xx, so raise_for_status() lets it
        through and we would go on to parse a non-payload as data.
        """
        if resp.status in AUTH_FAILURE_STATUSES:
            raise StopfinderAuthError(
                f"Stopfinder returned {resp.status} (token expired?)"
            )

    # -- 1. discovery ---------------------------------------------------------

    async def discover(self, email: str) -> str:
        """Resolve the district's API base URL from an email address."""
        url = URL(DISCOVERY_URL).with_query({"email": email})
        async with self._session.get(url, headers=self._base_headers()) as resp:
            resp.raise_for_status()
            body = (await resp.text()).strip()
        if not body.startswith("https://") or "transfinder.com" not in URL(body).host:
            raise StopfinderError(f"Unexpected discovery response: {body!r}")
        self.base_uri = body.rstrip("/")
        return self.base_uri

    # -- 2. login -------------------------------------------------------------

    async def login(self, username: str, password: str, device_id: str) -> Tokens:
        """Exchange credentials for tokens (grantType "password")."""
        return await self._post_token_request(
            {
                "grantType": "password",
                "username": username,
                "password": password,
                "deviceId": device_id,
                "rfApiVersion": RF_API_VERSION,
            }
        )

    async def refresh(
        self, username: str, refresh_token: str, device_id: str
    ) -> Tokens:
        """Renew the JWT without re-sending the password (grantType "refresh").

        The app's refreshLogin() posts to the same /tokens endpoint. The response
        carries a *new* refreshToken — it rotates, and the app persists it — so
        the caller must store what comes back or the next refresh will fail.
        """
        return await self._post_token_request(
            {
                "grantType": "refresh",
                "refreshToken": refresh_token,
                "username": username,
                "deviceId": device_id,
                "rfApiVersion": RF_API_VERSION,
            }
        )

    async def _post_token_request(self, payload: dict[str, Any]) -> Tokens:
        if not self.base_uri:
            raise StopfinderError("Call discover() before requesting a token.")
        headers = self._base_headers()
        headers["content-type"] = "application/json"
        async with self._session.post(
            f"{self.base_uri}{PATH_TOKENS}", headers=headers, json=payload
        ) as resp:
            if resp.status in (400, 401, 403):
                raise StopfinderAuthError("Stopfinder rejected the credentials")
            resp.raise_for_status()
            data = await resp.json()
        token = data.get("token")
        opaque = data.get("opaqueToken")
        if not token or not opaque:
            raise StopfinderError("Token response missing token(s)")
        self._token = token
        return Tokens(
            token=token,
            opaque_token=opaque,
            refresh_token=data.get("refreshToken"),
        )

    # -- 3. api versions / district identity ---------------------------------

    async def fetch_client_identity(self) -> dict[str, Any]:
        """Return the district record and cache client_keys / sf_client_id.

        The endpoint returns an array; a parent with children in two districts
        would get more than one entry. We take the first but keep the raw list.
        """
        async with self._session.get(
            f"{self.base_uri}{PATH_APIVERSIONS}", headers=self._auth_headers()
        ) as resp:
            self._raise_for_auth(resp)
            resp.raise_for_status()
            data = await resp.json()
        if not data:
            raise StopfinderError("apiversions returned no clients")
        first = data[0]
        self.client_id = str(first.get("clientId", ""))
        # The app lowercases client ids when it builds X-Client-Keys.
        self.client_keys = self.client_id.lower()
        self.sf_client_id = first.get("sfClientId") or first.get("id")
        if first.get("sfApiUri"):
            self.base_uri = str(first["sfApiUri"]).rstrip("/")
        return {"clients": data, "primary": first}

    # -- 4. subscriber --------------------------------------------------------

    async def fetch_subscriber(self) -> dict[str, Any]:
        async with self._session.get(
            f"{self.base_uri}{PATH_SUBSCRIBER}", headers=self._auth_headers()
        ) as resp:
            self._raise_for_auth(resp)
            resp.raise_for_status()
            return await resp.json()

    # -- 5. students / schedule ----------------------------------------------

    async def fetch_students(
        self, day: date | None = None
    ) -> list[StudentSchedule]:
        """Fetch the roster for a two-day window around `day` (defaults to today).

        Returns only the schedule block for `day`. On a non-service day the API
        returns the student with an empty `trips` list rather than omitting it.
        """
        day = day or date.today()
        params = {
            "dateStart": day.isoformat(),
            "dateEnd": (day + timedelta(days=1)).isoformat(),
        }
        url = URL(f"{self.base_uri}{PATH_STUDENTS}").with_query(params)
        async with self._session.get(url, headers=self._auth_headers()) as resp:
            self._raise_for_auth(resp)
            resp.raise_for_status()
            data = await resp.json()
        return _parse_students(data, day)

    # -- geo alert notifications ----------------------------------------------

    async def fetch_geo_alerts(
        self,
        subscriber_id: int,
        requests: list[dict[str, Any]],
        language: str = DEFAULT_LANGUAGE,
    ) -> list[GeoAlert]:
        """Ask for the latest geo-alert notification per (rider, trip).

        `requests` is the app's payload shape — one entry per rider/trip for
        today, each `{riderId, subscriberId, tripId, dataSourceId}`. Entries with
        nothing to report come back without a `geoAlertNotification` (or are
        omitted entirely), and are skipped.
        """
        if not requests:
            return []
        url = URL(f"{self.base_uri}{PATH_GEO_ALERTS}{subscriber_id}").with_query(
            {"language": language}
        )
        headers = self._auth_headers()
        headers["content-type"] = "application/json"
        async with self._session.post(url, headers=headers, json=requests) as resp:
            self._raise_for_auth(resp)
            resp.raise_for_status()
            data = await resp.json()
        return _parse_geo_alerts(data)

    # -- announcements --------------------------------------------------------

    async def fetch_announcements(self) -> list[Announcement]:
        """Fetch district announcements, newest first.

        Account-wide rather than per-student, and unfiltered: read and archived
        notices come back too, so the caller decides what counts as current.
        """
        async with self._session.get(
            f"{self.base_uri}{PATH_ANNOUNCEMENTS}", headers=self._auth_headers()
        ) as resp:
            self._raise_for_auth(resp)
            resp.raise_for_status()
            data = await resp.json()
        return _parse_announcements(data)

    # -- live position --------------------------------------------------------

    async def fetch_gps(self, group_name: str) -> GpsFix | None:
        """Poll GET /gps?groupName=... for the current bus position.

        The app calls this getLastBusLocation: it is a *last known* position with
        no notion of whether the route is running, so it will happily return
        yesterday's fix. That is why freshness, not presence, decides
        availability. Freshness is the caller's concern (use GpsFix.is_fresh()).

        Returns None when the endpoint has no usable fix (missing/zero coords).
        """
        url = URL(f"{self.base_uri}{PATH_GPS}").with_query({"groupName": group_name})
        async with self._session.get(url, headers=self._auth_headers()) as resp:
            self._raise_for_auth(resp)
            resp.raise_for_status()
            data = await resp.json()
        return _parse_gps(data)


# ---------------------------------------------------------------------------
# parsing helpers
# ---------------------------------------------------------------------------


def _jwt_exp(token: str) -> datetime | None:
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)  # pad base64url
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        if "exp" in payload:
            return datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
    except Exception:  # noqa: BLE001 - best effort only
        return None
    return None


def _parse_dt(value: str | None, tz_offset_minutes: float) -> datetime | None:
    """Parse a naive local timestamp and attach the district offset.

    Stopfinder serializes times as district-local with no `Z`. timeZoneMinutes
    from /students gives the offset (e.g. -240 for EDT).
    """
    if not value:
        return None
    naive = datetime.fromisoformat(value)
    tz = timezone(timedelta(minutes=tz_offset_minutes))
    return naive.replace(tzinfo=tz)


def _parse_utc(value: str | None) -> datetime | None:
    """Parse a .NET timestamp from a geo-alert notification as UTC.

    Unlike /students, whose times are naive district-local, these are UTC. In a
    capture from a UTC-4 district the response's own `date:` header read
    11:35:03 GMT against a `sentOn` of 11:30:12 — read as district-local that
    would place the alert about four hours in the future, so it can only be UTC.

    .NET also emits 7 fractional digits, one more than datetime accepts pre-3.11.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        head, _, frac = value.partition(".")
        if not frac:
            return None
        try:
            parsed = datetime.fromisoformat(f"{head}.{frac[:6]}")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_geo_alerts(data: list[dict[str, Any]] | None) -> list[GeoAlert]:
    """Parse the GeoAlertNotifications response into alerts worth reporting."""
    out: list[GeoAlert] = []
    for entry in data or []:
        note = entry.get("geoAlertNotification")
        if not note:
            continue  # nothing has fired for this rider/trip yet
        # The payload carries both `id` and `Id`; they are distinct JSON keys.
        alert_id = note.get("id") or note.get("Id")
        if alert_id in (None, ""):
            continue  # without an id we cannot tell a repeat from a new alert
        out.append(
            GeoAlert(
                alert_id=str(alert_id),
                rider_id=_as_int(entry.get("riderId") or note.get("riderId")),
                trip_id=_as_int(entry.get("tripId") or note.get("tripId")),
                zone_name=note.get("name") or "",
                subject=note.get("subject") or "",
                body=note.get("body") or "",
                sent_on=_parse_utc(note.get("sentOn")),
                created_at=_parse_utc(note.get("createTime")),
                alert_type=note.get("alertType"),
                raw=note,
            )
        )
    return out


def _parse_announcements(data: list[dict[str, Any]] | None) -> list[Announcement]:
    """Parse announcements, newest first.

    Timestamps are UTC, as with geo alerts. A capture proves it: an announcement
    whose body read "0745 11/10/2025" carried sentOn 12:46 — 07:46 in that
    UTC-5 district, matching its own text to the minute.
    """
    out: list[Announcement] = []
    for item in data or []:
        # Both `id` and `Id` are present; they are distinct JSON keys.
        announcement_id = item.get("id") or item.get("Id")
        if announcement_id in (None, ""):
            continue  # without an id we cannot tell a repeat from a new notice
        out.append(
            Announcement(
                announcement_id=str(announcement_id),
                subject=item.get("subject") or item.get("name") or "",
                body=item.get("body") or "",
                name=item.get("name") or "",
                sent_on=_parse_utc(item.get("sentOn")),
                opened_on=_parse_utc(item.get("openedOn")),
                sent_by_name=item.get("sentByName") or "",
                read=bool(item.get("read")),
                archived=bool(item.get("archived")),
                raw=item,
            )
        )
    # Undated notices sort last rather than blowing up on a None comparison.
    undated = datetime.min.replace(tzinfo=timezone.utc)
    out.sort(key=lambda a: a.sent_on or undated, reverse=True)
    return out


def _as_int(value: Any) -> int:
    """Coerce an id that may arrive as a number or a string."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _parse_gps(data: dict[str, Any] | None) -> GpsFix | None:
    """Parse a /gps response into a GpsFix, or None if there's no usable fix.

    Sample: {"rowId":0,"startTime":"2026-08-10T08:00:08",
             "timestamp":1786363208,"longitude":-74.0060,"latitude":40.7128}
    """
    if not data:
        return None
    lat = data.get("latitude")
    lon = data.get("longitude")
    ts = data.get("timestamp")
    if lat in (None, 0, 0.0) and lon in (None, 0, 0.0):
        return None  # no fix / null island
    if ts is None:
        return None
    try:
        fix_time = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        return GpsFix(
            latitude=float(lat),
            longitude=float(lon),
            fix_time=fix_time,
            raw=data,
        )
    except (TypeError, ValueError):
        return None


def _parse_students(data: list[dict], day: date) -> list[StudentSchedule]:
    out: list[StudentSchedule] = []
    for day_block in data:
        block_day = datetime.fromisoformat(day_block["date"]).date()
        if block_day != day:
            continue
        for sched in day_block.get("studentSchedules", []):
            tz_min = float(sched.get("timeZoneMinutes", 0.0))
            trips: list[Trip] = []
            for t in sched.get("trips", []):
                start = _parse_dt(t.get("startTime"), tz_min)
                finish = _parse_dt(t.get("finishTime"), tz_min)
                if start is None or finish is None:
                    continue
                trips.append(
                    Trip(
                        trip_id=t["id"],
                        name=t.get("name", ""),
                        bus_number=str(t.get("busNumber", "")),
                        vehicle_id=t.get("vehicleId", 0),
                        to_school=bool(t.get("toSchool")),
                        start_time=start,
                        finish_time=finish,
                        pickup_stop_id=t.get("pickUpStopId", 0),
                        dropoff_stop_id=t.get("dropOffStopId", 0),
                        pickup_stop_name=t.get("pickUpStopName", ""),
                        dropoff_stop_name=t.get("dropOffStopName", ""),
                        before_trip_min=int(sched.get("beforeTrip", 15)),
                        after_trip_min=int(sched.get("afterTrip", 15)),
                        adjust_minutes=int(t.get("adjustMinutes") or 0),
                        pickup_time=_parse_dt(t.get("pickUpTime"), tz_min),
                        dropoff_time=_parse_dt(t.get("dropOffTime"), tz_min),
                    )
                )
            out.append(
                StudentSchedule(
                    rider_id=sched["riderId"],
                    first_name=sched.get("firstName", ""),
                    last_name=sched.get("lastName", ""),
                    school=sched.get("school", ""),
                    grade=str(sched.get("grade") or ""),
                    client_id=sched.get("clientId", 0),
                    data_source_id=sched.get("dataSourceId", 0),
                    display_vehicle_on_map=bool(sched.get("displayVehicleOnMap", True)),
                    tz_offset_minutes=tz_min,
                    trips=trips,
                )
            )
    return out
