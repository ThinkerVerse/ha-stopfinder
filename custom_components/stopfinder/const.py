"""Constants for the Stopfinder integration.

Values here are taken directly from observed Stopfinder (Transfinder) app traffic.
Live position is read from a simple REST endpoint (GET /gps?groupName=...), which
the app polls on a timer. The SignalR VehicleEventHub exists too, but the REST
endpoint is simpler, uses the same auth as every other call, and is what this
integration relies on.
"""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "stopfinder"

# --- Discovery (unauthenticated: email -> per-district API base URL) ---------
DISCOVERY_URL: Final = "https://www.mytransfinder.com/$xcom/getStopfinder.asp"

# --- App identity headers the API expects ------------------------------------
APP_VERSION: Final = "3.1.0"          # sent as x-stopfinderapp-version
RF_API_VERSION: Final = "1.1"         # sent in the token request body

# Angular HttpClient default UA from the WebView; some servers sniff this.
USER_AGENT: Final = (
    "Mozilla/5.0 (Linux; Android 10; Build/QQ3A.200605.001; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
    "Chrome/81.0.4044.117 Mobile Safari/537.36"
)

# --- REST paths (relative to the discovered base URL) ------------------------
PATH_TOKENS: Final = "/tokens"
PATH_APIVERSIONS: Final = "/systems/apiversions"
PATH_SUBSCRIBER: Final = "/action/subscribers/current"
PATH_STUDENTS: Final = "/students"
PATH_GPS: Final = "/gps"              # GET /gps?groupName={clientId}_{dataSourceId}_{busNumber}
# POST /GeoAlertNotifications/{subscriberId}?language=xx
PATH_GEO_ALERTS: Final = "/GeoAlertNotifications/"
PATH_ANNOUNCEMENTS: Final = "/announcementssent"

# --- Derived GPS status (the /gps payload has NO status field) ----------------
# The app derives status client-side too, and writes it onto the trip object as
# `gpsStatus`. These are its four values, verbatim from the bundle's enum:
#   Searching = 0, NotAvailable = 1, NoVehicleAssigned = 2, ValidGPS = 3
GPS_VALID: Final = "ValidGPS"
GPS_SEARCHING: Final = "Searching"
GPS_NOT_AVAILABLE: Final = "NotAvailable"
GPS_NO_VEHICLE: Final = "NoVehicleAssigned"

# Every state the GPS-status sensor can report, declared as its enum options.
GPS_STATUSES: Final = [
    GPS_VALID,
    GPS_SEARCHING,
    GPS_NOT_AVAILABLE,
    GPS_NO_VEHICLE,
]

# --- Auth ---------------------------------------------------------------------
# The app's HTTP interceptor refreshes on 401 *and* 203. 203 is a 2xx, so it
# never trips raise_for_status() — it has to be checked explicitly.
AUTH_FAILURE_STATUSES: Final = (203, 401)
# Refresh once the JWT is within this margin of expiry.
TOKEN_REFRESH_MARGIN_SECONDS: Final = 300

# --- Config entry keys -------------------------------------------------------
CONF_USERNAME: Final = "username"
CONF_PASSWORD: Final = "password"
CONF_DEVICE_ID: Final = "device_id"
CONF_REFRESH_TOKEN: Final = "refresh_token"
CONF_BASE_URI: Final = "base_uri"
CONF_CLIENT_KEYS: Final = "client_keys"
CONF_SF_CLIENT_ID: Final = "sf_client_id"
CONF_SUBSCRIBER_ID: Final = "subscriber_id"

# --- Options (per config entry, editable after setup) -------------------------
CONF_GPS_POLL_SECONDS: Final = "gps_poll_seconds"

# --- Behaviour tuning --------------------------------------------------------
# Only poll /gps while a trip's window is open:
#   [startTime - beforeTrip, finishTime + afterTrip]
DEFAULT_BEFORE_TRIP_MIN: Final = 15
DEFAULT_AFTER_TRIP_MIN: Final = 15

# How often to re-evaluate which trips are active (open/close the poll window).
SCHEDULE_TICK_SECONDS: Final = 60

# How often to poll /gps while at least one trip is active.
#
# The app's own REST poll is 60s, but that is only the *fallback* it degrades to
# when its SignalR hub is disconnected — normally it receives pushed vehicle
# events as they happen, which is why the app's map moves more often than once a
# minute. Being REST-only, matching 60s would make the app's worst case our
# normal case, so we poll faster than that during a live trip and not at all
# outside one. Tunable per config entry; the timer only exists while a window is
# open.
GPS_POLL_SECONDS: Final = 10
GPS_POLL_SECONDS_MIN: Final = 10
GPS_POLL_SECONDS_MAX: Final = 300
# A fix older than this is treated as stale -> entity unavailable. Guards against
# the endpoint returning a last-known/yesterday position outside a live run.
# 300s is the app's own constant: it drops the bus from the map with the reason
# "Has not received vehicle events in 5 minutes".
GPS_STALE_AFTER_SECONDS: Final = 300

# --- Geo alerts ---------------------------------------------------------------
# The app refreshes these on interval(4 * CACHE_LIFETIME_MINUTES * 1000) with
# CACHE_LIFETIME_MINUTES = 15, i.e. every 60s — the same cadence as our schedule
# tick, which is where the poll is hung.
DEFAULT_LANGUAGE: Final = "en"

# Fired on the HA event bus when a geo alert we have not seen before arrives.
EVENT_GEO_ALERT: Final = "stopfinder_geo_alert"

# --- Announcements ------------------------------------------------------------
# District-wide notices ("bus 233 running 20 minutes late"). Rare, but the ones
# that matter arrive in the morning, so they are polled on their own slow timer
# rather than with the roster. The app has no cadence to copy: it refetches on
# app resume and on UI navigation, never on an interval.
ANNOUNCEMENT_POLL_MINUTES: Final = 15
ANNOUNCEMENT_POLL_MINUTES_MIN: Final = 5
ANNOUNCEMENT_POLL_MINUTES_MAX: Final = 1440
CONF_ANNOUNCEMENT_POLL_MINUTES: Final = "announcement_poll_minutes"

EVENT_ANNOUNCEMENT: Final = "stopfinder_announcement"

PLATFORMS: Final = ["device_tracker", "sensor"]
