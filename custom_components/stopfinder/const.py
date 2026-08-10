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

# --- Derived GPS status (the /gps payload has NO status field) ----------------
# We infer these from whether a fresh fix is present.
GPS_VALID: Final = "ValidGPS"
GPS_NO_SIGNAL: Final = "NoSignal"

# --- Config entry keys -------------------------------------------------------
CONF_USERNAME: Final = "username"
CONF_PASSWORD: Final = "password"
CONF_DEVICE_ID: Final = "device_id"
CONF_BASE_URI: Final = "base_uri"
CONF_CLIENT_KEYS: Final = "client_keys"
CONF_SF_CLIENT_ID: Final = "sf_client_id"
CONF_SUBSCRIBER_ID: Final = "subscriber_id"

# --- Behaviour tuning --------------------------------------------------------
# Only poll /gps while a trip's window is open:
#   [startTime - beforeTrip, finishTime + afterTrip]
DEFAULT_BEFORE_TRIP_MIN: Final = 15
DEFAULT_AFTER_TRIP_MIN: Final = 15

# How often to re-evaluate which trips are active (open/close the poll window).
SCHEDULE_TICK_SECONDS: Final = 60
# How often to poll /gps while at least one trip is active. The app uses a
# similar cadence; keep it modest to stay unobtrusive.
GPS_POLL_SECONDS: Final = 15
# A fix older than this is treated as stale -> entity unavailable. Guards against
# the endpoint returning a last-known/yesterday position outside a live run.
GPS_STALE_AFTER_SECONDS: Final = 300

PLATFORMS: Final = ["device_tracker", "sensor"]
