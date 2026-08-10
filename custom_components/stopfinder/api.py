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
    DISCOVERY_URL,
    PATH_APIVERSIONS,
    GPS_STALE_AFTER_SECONDS,
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
    trips: list[Trip] = field(default_factory=list)

    def group_name(self, trip: Trip) -> str:
        """Vehicle-hub group: {clientId}_{dataSourceId}_{busNumber}."""
        return f"{self.client_id}_{self.data_source_id}_{trip.bus_number}"


class StopfinderApi:
    """Thin async wrapper over the Stopfinder REST endpoints."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        self.base_uri: str | None = None
        self.client_keys: str = ""
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
        # The app lowercases client ids when it builds X-Client-Keys.
        self.client_keys = str(first.get("clientId", "")).lower()
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
                    )
                )
            out.append(
                StudentSchedule(
                    rider_id=sched["riderId"],
                    first_name=sched.get("firstName", ""),
                    last_name=sched.get("lastName", ""),
                    school=sched.get("school", ""),
                    client_id=sched.get("clientId", 0),
                    data_source_id=sched.get("dataSourceId", 0),
                    display_vehicle_on_map=bool(sched.get("displayVehicleOnMap", True)),
                    tz_offset_minutes=tz_min,
                    trips=trips,
                )
            )
    return out
