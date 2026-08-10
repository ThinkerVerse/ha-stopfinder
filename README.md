# Home Assistant — Stopfinder (Transfinder) custom integration

Tracks your child's school bus as a `device_tracker`, plus GPS-status, bus-number,
and active-trip sensors, using the same undocumented Stopfinder API the mobile app
uses.

> Reverse-engineered from observed traffic and the app bundle. No official API
> exists. Automated access may violate Stopfinder's ToS — the realistic risk is
> your account being disabled, which also breaks the app for you. This
> integration polls only during route windows, and not at all outside them.

## How it works

1. **Discovery** — `GET mytransfinder.com/$xcom/getStopfinder.asp?email=…`
   returns your district's API base URL as plain text.
2. **Login** — `POST {base}/tokens` with `grantType: "password"` returns a JWT
   (`token`, used in a custom `token:` header for every REST call) plus a
   refresh token.
3. **Identity** — `GET {base}/systems/apiversions` yields `clientId`
   (`x-client-keys`) and `sfClientId`.
4. **Roster** — `GET {base}/students?dateStart&dateEnd` gives each student's
   trips, stops, bus number, `dataSourceId`, `timeZoneMinutes`, and the
   before/after padding that defines the route window.
5. **Live position** — `GET {base}/gps?groupName={clientId}_{dataSourceId}_{busNumber}`
   returns `{latitude, longitude, timestamp}`. The coordinator polls this every
   10s (configurable) while a trip window is open. Outside a window the timer
   does not exist at all, so nothing is requested.

## Why REST polling and not SignalR?

The app's primary source is a SignalR hub, and `/gps` is only its fallback. An
early version of this integration did try the hub (via `pysignalr`) and got no
data; the `/gps` capture then made it unnecessary. The bundle explains what that
attempt was up against — connecting means getting *all* of the following right:

- **`wss://{base}/VehicleEventHub?stopfinderadminversion=2.0&groupName={group}`**,
  with `skipNegotiation: true` and the WebSockets transport, so there is no
  `/negotiate` POST to discover anything from. Clients that always negotiate
  cannot talk to it at all.
- **MessagePack**, not JSON (`{name: "messagepack", version: 1}`), so frames are
  binary.
- **The `opaqueToken`, not the JWT**, passed as an `access_token` query
  parameter. Using the REST token here fails auth — an easy and silent mistake.
- **`keepAliveIntervalInMilliseconds: 10000` against a
  `serverTimeoutInMilliseconds: 60000`**, so a client that does not ping gets
  dropped.
- Group membership is implicit in the query string: `VehicleEventHub` has no join
  call. (Only `RealTimeUpdateHub` exposes `Subscribe`/`Unsubscribe` invokes.)
  Server→client method is `ReceiveVehicleEvents`, payload at `data[0]`.

What it buys is **latency, not data**: the frames carry the same
`Timestamp`/`Latitude`/`Longitude` the REST endpoint returns — the app remaps the
REST response into that exact shape so both paths feed one code path — and speed
and heading are absent from the app entirely. Against that, it costs a runtime
dependency (this integration currently has none, which matters for HACS), a
persistent socket to supervise, reconnect handling, and a binary protocol to keep
working across app updates. At a 10s poll the gap is small enough that REST wins
on robustness. If sub-10s tracking or `RealTimeUpdates` ETAs ever become the
goal, the hub is the way — the details above are the map.

## Entities (per student)

- **device_tracker "Bus"** — live GPS position; available only when the latest
  fix is fresh (< 5 min old). Attributes: bus number, trip, direction, pickup /
  dropoff stop, reported-at time.
- **GPS status** — derived, using the app's own four values:
  | State | Meaning |
  |---|---|
  | `ValidGPS` | A fresh fix is in hand. |
  | `Searching` | Trip is running, waiting on the first report. |
  | `NotAvailable` | Trip is running but nothing fresh has arrived for 5 minutes. |
  | `NoVehicleAssigned` | The trip has no bus assigned, so there is nothing to track. |
  | *unknown* | Outside every trip window. |
- **Bus number** — current `busNumber`; a separate entity because substitutions
  change it (useful to alert on).
- **Active trip** — trip name (also encodes AM/PM and direction).

## Upgrading to 1.0.0

**Breaking:** the GPS-status sensor no longer reports `NoSignal`. It now uses the
app's own vocabulary, so any automation matching on `NoSignal` needs updating —
`NotAvailable` is the closest equivalent. The entity, its id and its history are
unchanged; only the state strings are.

The poll interval is now a per-entry option (**Configure** on the integration
card), defaulting to 10s while a trip is running.

## Design notes

**Freshness, not presence, decides availability.** The `/gps` payload has no
status, speed, or heading field — only a position and a Unix-epoch `timestamp`.
The app calls this endpoint `getLastBusLocation` and it has no notion of whether
a route is running, so it will happily hand back a stale fix. Availability is
therefore gated on age (`GPS_STALE_AFTER_SECONDS`, 300). That is not a guess:
300s is the app's own constant, and it drops the bus from its map with the reason
*"Has not received vehicle events in 5 minutes"*.

**Poll cadence, and why it isn't 60s.** The app's REST poll of `/gps` is on a
60s timer — `interval(60000)`, the only polling cadence in the whole bundle — but
that is the *fallback* half of `iif(connected, signalRStream, polling)`. Normally
the app receives pushed vehicle events over its SignalR hub as they happen, which
is why its map moves more often than once a minute. Being REST-only, adopting 60s
would make the app's degraded case our normal case, so the default is **10s while
a trip is running** and nothing at all outside a window.

The window state, not a flag, controls the timer: the schedule tick arms it when a
window opens (polling immediately, as the app does with `startWith(0)`) and
disarms it when the last window closes. Set **Configure → Seconds between
position checks** to trade responsiveness against request volume (10-300s).

**Route windows follow the app's `isTripRunning`.** The trip's own
`adjustMinutes` shifts both ends of the window first, then the student's
`beforeTrip` / `afterTrip` pad them. Times from `/students` are naive
district-local and get `timeZoneMinutes` attached (not converted); the `/gps`
`timestamp` is epoch UTC.

**Requests are deduplicated by bus.** Siblings on the same bus share one
`groupName`, so they share one request per poll — the app caches by endpoint for
the same reason.

**Entity ids key on `riderId`, never bus number,** because
`vehicleSubstitutionEnabled` means the bus can change day to day.

**Tokens.** The JWT's envelope `expiration` is .NET `DateTime.MinValue`, so the
real TTL comes from decoding the `exp` claim. Renewal uses the refresh grant
(`grantType: "refresh"`), falling back to a full re-login only if the refresh
token is rejected. The refresh token **rotates on every use**, so the newest one
is persisted to the config entry. A rejected password raises a reauth flow rather
than failing silently.

## Known gaps / TODOs

- **Multi-district parents**: `apiversions` returns an array; only the first
  client is used. The app groups the array by `sfApiUri`, comma-joins the
  `clientId`s into a single lowercased `X-Client-Keys` per host, fans out across
  distinct hosts, and merges the day-blocks by date.
- **ETA is not implemented.** `POST {base}/realtimeupdates/multiple` takes an
  array of `{clientId}_{dataSourceId}_{tripId}_{stopId}` group names and returns
  `etaInMinutes`, `etA_TimeStamp`, `planned_TimeStamp` and `onTime` per stop —
  a better automation trigger than raw coordinates. It is gated on the student's
  `enableEtaAlerts` and on the district reporting `supportedProductVersion`
  `v2.5`.
- **Scan events are not implemented.** `GET {base}/action/scannedrecords/current?date=`
  exposes actual scan-on / scan-off records, i.e. whether the child boarded.
- **Speed / heading** are genuinely absent — not just from `/gps`, but from the
  app, whose only `heading` handling is for the phone's own location arrow.
- **Server-side version gating exists.** The app sends
  `X-StopfinderApp-Version: 3.1.0` (still 3.1.0 as of app build 3.1.5) and the
  server can force an upgrade, nulling the token. If Transfinder raises the
  minimum, this integration will start failing auth until `APP_VERSION` is bumped.
- **Push device registration**: the login/refresh response's `lastDeviceId` is
  what the app keys push re-registration on, not session eviction — so signing in
  here should not sign your phone out, but it may make the phone re-register its
  push device. This integration uses its own random `deviceId` and keeps it
  stable across reauth.

## Install

Copy `custom_components/stopfinder` into your Home Assistant
`config/custom_components/` directory, or add this repo to HACS:

1. HACS → three-dot menu → **Custom repositories**.
2. Add `https://github.com/ThinkerVerse/ha-stopfinder`, category **Integration**.
3. Install **Stopfinder**, then restart Home Assistant.
4. Settings → Devices & Services → **Add Integration** → Stopfinder, and sign in
   with your Stopfinder email and password.

**Configure** on the integration card exposes the position-check interval
(default 10s, range 10-300s), which applies only while a trip window is open.

The manifest must end up at exactly
`config/custom_components/stopfinder/manifest.json`. Dropping the whole repo
folder in nests it one level too deep, and the only symptom is the frontend
saying *"Config flow could not be loaded: Invalid handler specified."* — which is
the generic message for any import-time failure.

## Tests

```bash
pip install pytest aiohttp
python3 -m pytest tests/ -q
```

The suite covers roster and `/gps` parsing, route-window maths (including
`adjustMinutes`), the derived GPS-status matrix, request deduplication, and an
import smoke test for the whole package.

## Scrub before publishing

Remove captured tokens, your email, subscriber/rider IDs, and home coordinates
from commits, issues, and traffic dumps. `.gitignore` already excludes `*.mitm`,
flow exports, APKs, and source maps.

### Versioning / releases

```bash
# after committing changes and bumping manifest.json "version"
git tag v1.0.0
git push origin main --tags
# then create a GitHub Release from that tag; HACS will offer it as an update
```
