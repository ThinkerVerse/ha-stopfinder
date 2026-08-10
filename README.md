# Home Assistant — Stopfinder (Transfinder) custom integration

Tracks your child's school bus as a `device_tracker`, plus GPS-status, bus-number,
and active-trip sensors, using the same undocumented Stopfinder API the mobile app
uses.

> Reverse-engineered from observed traffic. No official API exists. Automated
> access may violate Stopfinder's ToS — the realistic risk is your account being
> disabled, which also breaks the app for you. This integration polls only during
> route windows.

## How it works

1. **Discovery** — `GET mytransfinder.com/$xcom/getStopfinder.asp?email=…`
   returns your district's API base URL as plain text.
2. **Login** — `POST {base}/tokens` returns a JWT (`token`, used in a custom
   `token:` header for every REST call).
3. **Identity** — `GET {base}/systems/apiversions` yields `clientId`
   (`x-client-keys`) and `sfClientId`.
4. **Roster** — `GET {base}/students?dateStart&dateEnd` gives each student's
   trips, stops, bus number, `dataSourceId`, and `timeZoneMinutes`.
5. **Live position** — `GET {base}/gps?groupName={clientId}_{dataSourceId}_{busNumber}`
   returns `{latitude, longitude, timestamp}`. The coordinator polls this every
   15s while a trip window is open and idles otherwise.

The `/gps` endpoint replaced an earlier SignalR/WebSocket approach: it's the same
host and auth as every other call, so there's no WebSocket, MessagePack, or
negotiate handshake to get wrong. (The SignalR `VehicleEventHub` still exists in
the app but isn't needed.)

## Entities (per student)

- **device_tracker "Bus"** — live GPS position; available only when the latest
  fix is fresh (< 5 min old). Attributes: bus number, trip, direction, pickup /
  dropoff stop, reported-at time.
- **GPS status** — derived: `ValidGPS` when a fresh fix is present, `NoSignal`
  while a trip is active but no fresh fix has arrived, unknown outside any window.
- **Bus number** — current `busNumber`; a separate entity because substitutions
  change it (useful to alert on).
- **Active trip** — trip name (also encodes AM/PM and direction).

## Design notes / freshness

The `/gps` payload has **no** status, speed, or heading field — only a position
and a Unix-epoch `timestamp`. Availability is therefore derived from freshness
(`GPS_STALE_AFTER_SECONDS`, default 300). This guards against the endpoint
returning a stale last-known position outside a live run. If you observe the
off-window response returning zeros or an old fix, the guard already handles it;
adjust the threshold in `const.py` if buses in your district report less often.

## Known gaps / TODOs

- **Token refresh** isn't implemented (the refresh call was never captured); the
  coordinator re-logs-in when the JWT nears expiry. Capture it to avoid churn and
  check whether the refresh token rotates.
- **Single-session risk**: `lastDeviceId` in the login response hints the server
  may track one device. This integration uses its own random `deviceId`; verify
  logging in here doesn't sign your phone out.
- **Multi-district parents**: `apiversions` returns an array; only the first
  client is used today.
- **Speed / heading** aren't in `/gps`. If you want them, the SignalR
  `ReceiveVehicleEvents` frames may carry more — but that path is more fragile.

## Install

Copy `custom_components/stopfinder` into your HA `config/custom_components/`
directory (or add as a HACS custom repository), restart, then add the integration
from Settings → Devices & Services and sign in with your Stopfinder email and
password.

## Scrub before publishing

Remove captured tokens, your email, subscriber/rider IDs, and home coordinates
from commits, issues, and traffic dumps. Add `*.mitm` and flow exports to
`.gitignore`.

## Install via HACS (recommended)

1. HACS → three-dot menu → **Custom repositories**.
2. Add `https://github.com/ThinkerVerse/ha-stopfinder`, category **Integration**.
3. Install **Stopfinder**, then restart Home Assistant.
4. Settings → Devices & Services → **Add Integration** → Stopfinder, and sign in.

Updates appear in HACS whenever you publish a new GitHub **release** whose tag
matches the `version` in `manifest.json` (e.g. tag `v0.2.0`).

### Versioning / releases

```bash
# after committing changes and bumping manifest.json "version"
git tag v0.2.0
git push origin main --tags
# then create a GitHub Release from that tag; HACS will offer it as an update
```
