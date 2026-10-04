# Kumo Cloud API v3

> [!CAUTION]
> **This document is currently out of date.**
> As of pykumo 0.5.0, the library has been fully migrated to the V3 API for all credential retrieval, and the legacy V2 API is no longer used. This document remains as a historical reference for the reverse-engineering process.

**Note**: This API summary is reverse-engineered and is a work in progress.
 This is not official documentation from Mitsubishi, nor has their permission been sought to publish these findings.

## Summary
The Mitsubishi Kumo Cloud API as used by its mobile apps has changed from a functional but quirky and dated version (with no published version number, as far as I can tell) to a v3 API that uses a more modern approach, with such features as refreshable [JWT](https://en.wikipedia.org/wiki/JSON_Web_Token) authentication and servers capable of using [HTTP/2](https://en.wikipedia.org/wiki/HTTP/2) connections.

The old API is used by PyKumo solely to obtain sufficient information to communicate with indoor units via their (even more quirky) local http interface. As of April 2, 2025 this old API seems to be still working but serving stale data; the new API is what's being updated as customers' information changes.

This document is published in an effort to discover enough of the v3 API to allow pykumo to continue to monitor and control users' indoor units via their local API. It could also serve as a guide for developing a library that monitors and controls units via the cloud.

### Remaining to be documented

Portions of the v3 API are not yet documented:
- PATCH bodies other than zone rename and filter reset (holds, schedules, group settings)
- Writes to the `/v4/` schedule endpoints
- `/v3/accounts/me` in detail

## WebSocket interface
More information -- including the indoor unit password -- is available via a WebSocket interface. [HA-Kumo-WS](https://github.com/EnumC/ha_kumo_ws) is an entirely cloud-based integration and has examples of using this WebSocket.

### Observed in an October 2026 capture
A traffic capture (pykumo 0.5.3, app version `3.2.4`, adapter firmware `02.06.26`) showed:

- **No credentials from the cloud.** `/v3/devices/{serial}/status` no longer included `cryptoSerial` (or `cryptoKeySet`), and the `adapter_update` sent in reply to `force_adapter_request` had no `password`. Local control kept working only because credentials were cached from an earlier setup. The adapter's own `adapter/status` still includes its password, but you need the password to ask for it.
- **Handshake timing.** `socket-prod.kumocloud.com` uses Engine.IO 4 over long polling with `pingInterval` 25000 and `pingTimeout` 20000. With nothing else to send, a poll stays open until the next ping, about 25 s, so the poll's read timeout must be longer than that.
- **Event sequence.** `subscribe ["", <user-id>]` gets `subscribed`; `subscribe [<serial>]` gets an immediate full `device_update`; `force_adapter_request [<serial>, "adapterStatus"]` gets an `adapter_update` about a second later; `device_status_v2 [<serial>]` gets connection details (`status`, `lastTimeConnected`, `lastDisconnectedReason`, `serverId`, `hasIduCommunicationError`). `device_status_v2 [""]` gets back an empty `{"deviceSerial": ""}`.
- **`adapter_update` fields:** `firmwareVersion`, `roomTempDisplayOffset`, `routerSsid`, `routerRssi`, `minSetpoint`/`maxSetpoint` (also spelled `minSetPoint`/`maxSetPoint`), `lastUpdated`.
- **`device_update` fields** match `devices/{device-serial}` below, plus `realValues`, `collectMethod` and `date`. A partial `device_update` (only the changed operating values) follows each `device_status_v2`.
- **Setpoint limits.** The status endpoint's `minSetPoint`/`maxSetPoint` (19.5/28 here) match the local `adapter/status` fields `userMinCoolSetPoint`/`userMaxHeatSetPoint`: limits set in the app, which can be narrower than the profile's `minimumSetPoints`/`maximumSetPoints` (cool 19-30, heat 17-28, auto 19-28 on the same unit). pykumo's `get_setpoint_limits()` combines the two.

### Observed in a capture of the Comfort app (October 2026)
A capture of the official iOS app (Comfort `3.5.0`, adapter firmware `02.06.26`) covering a full logout and login, mode and fan-speed changes, an LED toggle, a zone rename and a filter reset showed:

- **The app gets no local credentials either.** No request or response on either host, and no Socket.IO frame, carried a `cryptoSerial`, `cryptoKeySet` or adapter `password`. So their absence is not down to pykumo's request shape, its `appVersion`, or `isPoliciesAccepted` (still `false` after the app's own login, with no prompt). The app controls units through the cloud instead; see [Commands](#commands).
- **Three `force_adapter_request` types.** The app sends `adapterStatus`, `profile` and `iuStatus` together. `adapterStatus` gets an `adapter_update` as above; `profile` gets a `profile_update`, the same object as [`devices/{device-serial}/profile`](#devicesdevice-serialprofile) (not wrapped in a list); `iuStatus` gets a full `device_update`.
- **`unsubscribe [<serial>]`** gets `unsubscribed ["Successfully unsubscribed from: <serial>"]`.
- **`realValues` in `device_update`.** After a command, the top-level fields show the value just requested and `realValues` appears to hold what the unit is actually reporting until it catches up. For example, sending `fanSpeed: "quiet"` then `"low"` produced `"fanSpeed": "low", "realValues": {"fanSpeed": "quiet"}`; sending `operationMode: "dry"` then `"off"` produced `"operationMode": "off", "power": 0, "realValues": {"operationMode": "dry", "power": 1}`. It is `{}` when the two agree.
- **Transport.** The app opens with long polling and upgrades to a WebSocket (`2probe` → `3probe` → `5`). Long polling alone, as pykumo uses it, also works.

## Hostname
The scheme and hostname for all endpoints described below is https://app-prod.kumocloud.com/

## Base headers
All or most of the API endpoints seem to require, at a minimum, an `x-app-version` header with a value like `3.0.3`. The base headers that seem to work OK:
```
  Accept: application/json text/plain, */*
  Accept-Encoding: gzip, deflate, br
  Accept-Language: en-US, en
  x-app-version: 3.0.3
```

As of October 2026 the iOS app (version `3.5.0`) sends:
```
  Accept: application/json
  Accept-Encoding: gzip, deflate, br
  Accept-Language: en-CA,en;q=0.9
  Content-Type: application/json
  x-app-version: 3.5.0
  app-env: prd
  x-allow-cache: true
  User-Agent: kumocloud/2383 CFNetwork/3896.100.1.2.1 Darwin/27.0.0
```
plus Sentry tracing headers (`baggage`, `sentry-trace`). pykumo sends `x-app-version: 3.2.4` without the others and is served normally.

## Authorization

**Endpoints**
- Login: `/v3/login`
- Refresh: `/v3/refresh`

### Login

The **Login** endpoint is used for initial login, or if the refresh token has expired. POSTing a body as follows (with the base headers) returns a JSON response with access and refresh tokens.
```
{
  "username": "<users-kumo-username>",
  "password": "<users-kumo-password>",
  "appVersion": "3.0.3"
}
```

Example response:

```
{
  "id": "<redacted>",
  "username": "<redacted>",
  "email": "<redacted>",
  "firstName": "<redacted>",
  "lastName": "<redacted>",
  "phone": "<redacted>",
  "isPersonalAccount": true,
  "isEmailVerified": true,
  "isPoliciesAccepted": false,
  "token": {
    "access": "<standard-jwt-token>",
    "refresh": "<standard-jwt-token>"
  },
  "preferences": {
    "sendAnalytics": 1,
    "lastUpdate": <numeric-timestamp>
  },
  "company": null,
  "isSalesforceIntegrated": true
}
```

The access token is short-lived: 20 minutes (`exp - iat` = 1200 s).
The refresh token is long-lived. It was about a month in April 2025; as of October 2026 it is one year (`exp - iat` = 31557600 s). Both JWTs carry only `id`, `username`, `iat` and `exp`.

The October 2026 login response also has `scheduleVersion` (`"v2"`) and `hasDemoAccess`, and `preferences` holds the app's UI settings (see [Preferences](#preferences)).

#### JWT usage
The access token must be provided to all other API requests, in an Authorization header as follows. This is standard JWT usage.
```
  Authorization: Bearer <token-string>
```

### Refresh

If the access token has expired but the refresh token is still valid, a POST to the refresh endpoint with an `Authorization` header (as above) containing the refresh token will provide a response body as follows, bearing new access and refresh tokens. No username or password is required to refresh the tokens.

POST body:
```
{"refresh": "<refresh-token>"}
```
Response:
```
{
  "access": "<new-access-token>",
  "refresh": <new-refresh-token>"
}
```

Notably, the new refresh token will have a new expiration time in the future (a month in April 2025, a year as of October 2026), and the old refresh token will cease to work.

If the refresh token itself is expired (or not known), the Login endpoint (with username and password) may be used to obtain fresh tokens.

### Logout
`POST /v3/logout` with the access token and no body returns 200 with an empty body. Afterwards the token is refused (see [Errors](#errors)). Before logging out, the app unregisters its push token with `POST /v3/accounts/fcm/delete`.

## Account information

**Endpoints**
- Me: `GET /v3/accounts/me`
- Preferences: `PUT /v3/accounts/preferences`
- Push registration: `POST /v3/accounts/fcm`, `POST /v3/accounts/fcm/delete`
- App version gate: `GET /v3/config/new-version-overlay`

A GET of **Me** returns various account information, quite similar to the response to the initial Login POST.

Details to-be-documented.

### Preferences
The app's UI state (`celsius`, survey and walkthrough flags, `zoneAndGroupTilesOrder`, `isMinMaxSetpointsEnabled`, and so on). The app PUTs the whole object, and the response is the stored object.

### Push registration
`POST /v3/accounts/fcm` with `{"deviceToken": "<firebase-token>"}` registers the phone for push notifications; `POST /v3/accounts/fcm/delete` with the same body removes it. Both return `{"success": true}`.

### New version overlay
Fetched by the app at startup, before login. Probably a forced-upgrade switch.
```
{"active": false, "minimumVersion": null}
```

## Notifications

**Endpoints**
- `GET /v3/notifications/active/unseen-count`
- `GET /v3/notifications/active?page=1`
- `GET /v3/notifications/resolved?page=1`
- `PATCH /v3/notifications/seen`

**Unseen count** populates the red dot on the notification bell in the Comfort app:
```
{"unseenCount": 1}
```

**Active** and **resolved** are paged (`next`, `previous`, `count`, `data`):
```
{
    "next": null,
    "previous": null,
    "count": 1,
    "data": [
        {
            "id": "<notification-id>",
            "zoneId": "<zone-id>",
            "active": true,
            "severity": "INFO",
            "type": "APP_NOTIFICATION",
            "eventType": "filterReminder",
            "data": {
                "zoneId": "<zone-id>",
                "accountId": "<user-id>",
                "deviceSerial": "<device-serial>",
                "channel": "filterReminder",
                "requestType": "filter-reminder-notification"
            },
            "accountId": "<user-id>",
            "seen": false,
            "entityType": null,
            "entityId": null,
            "createdAt": "2026-09-14T07:29:02.385Z",
            "updatedAt": "2026-09-14T07:29:02.385Z",
            "resolvedAt": null,
            "zone": {"id": "<zone-id>", "name": "<redacted>"},
            "site": {"id": "<site-id>", "name": "<redacted>"}
        }
    ]
}
```

**Seen** marks notifications as read. Body `{"notificationIds": ["<notification-id>"]}`; response `{}`.

## Sites

**Endpoints**
- Collection: `/v3/sites/`
- Collection with addresses: `/v3/sites/full`
- Pending transfers: `/v3/sites/transfers/pending`

### Collection Endpoint
The **Collection** endpoint returns a list of "sites" associated with the login. Presumably these are separate installations perhaps at different addresses. These are called _Locations_ in the Comfort app.

Example:

```
[
  {
    "id": "<guid>",
    "name": "<redacted>",
    "isActive": true,
    "createdAt": "2025-03-30T13:09:59.571Z",
    "updatedAt": "2025-03-30T13:09:59.571Z",
    "schedulesEnabled": true,
    "notificationsEnabled": true,
    "favorite": false,
    "mak": null,
    "baseMAK": null
  }
]
```

The important information here is `id` which is the `{site-id}` for several of the remaining API calls.

As of October 2026 each site also has `role` (e.g. `"owner"`), `isDemo` and `demoEndsAt`.

### Collection with addresses
`/v3/sites/full` returns the same list with each site's postal address and `requiresAddressUpdate`, i.e. the same fields as [`sites/{site-id}`](#sitessite-id). Mind the street address when sharing captures.

### Pending Transfers
In the app, a Location can be transferred to a new owner. This is done by email address, and this endpoint allows the app to notify a user about an incoming transfer request. Returns `[]` when there are none.

## Site endpoints

**Endpoints**
- /v3/sites/{site-id}
- /v3/sites/{site-id}/kumo-station
- /v3/sites/{site-id}/zones
- /v3/sites/{site-id}/groups
- /v3/sites/{site-id}/weather
- /v3/sites/{site-id}/dr-programs
- /v3/sites/{site-id}/program-enroll
- /v4/sites/{site-id}/schedule-seasons

`{site-id}` is the `id` GUID returned from the `/v3/sites/` collection endpoint.

### sites/{site-id}
```
{
    "id": "<site-id>",
    "name": "<redacted>",
    "isActive": true,
    "createdAt": "2025-03-25T19:19:27.828Z",
    "updatedAt": "2025-04-08T19:10:33.928Z",
    "address": "<redacted>",
    "address2": "<redacted>",
    "city": "<redacted>",
    "state": "<redacted>",
    "zip": "<redacted>",
    "country": "<redacted>",
    "favorite": false,
    "schedulesEnabled": true,
    "notificationsEnabled": true,
    "requiresAddressUpdate": false,
    "mak": "<redacted?>",
    "baseMAK": null
}
```

### sites/{site-id}/groups

**minRuntime**: the minimum time the system should run in heating or cooling mode before switching.
Available values in the app: 10, 20, 30, 40 minutes

**maxStandby**: the maximum time your system should wait in heating or cooling mode before switching.
Available values in the app: 30 minutes, 1, 2, 3, 4 hours
```
[
    {
        "id": "<group-id>",
        "name": "<redacted>",
        "isActive": true,
        "createdAt": "2025-04-08T16:21:44.662Z",
        "updatedAt": "2025-04-08T16:25:01.306Z",
        "systemChangeoverEnabled": true,
        "minRuntime": 30,
        "maxStandby": 60
    }
]
```

### sites/{site-id}/zones
```
[
    {
        "id": "<zone-id>",
        "name": "First Floor",
        "isActive": true,
        "group": {
            "id": "<group-id>",
            "name": "<redacted>",
            "isActive": true,
            "createdAt": "2025-04-08T16:21:44.662Z",
            "updatedAt": "2025-04-08T16:25:01.306Z",
            "systemChangeoverEnabled": true,
            "minRuntime": 30,
            "maxStandby": 60
        },
        "adapter": {
            "id": "<adapter-id>",
            "deviceSerial": "<adapter-serial>",
            "isSimulator": false,
            "roomTemp": 22,
            "spCool": 23,
            "spHeat": 21.5,
            "spAuto": null,
            "humidity": 41,
            "scheduleOwner": "adapter",
            "power": 1,
            "operationMode": "autoHeat",
            "connected": true,
            "hasSensor": false,
            "hasMhk2": true,
            "timeZone": "America/Los_Angeles",
            "isHeadless": false,
            "lastStatusChangeAt": "2025-04-05T17:41:41.644Z",
            "createdAt": "2025-03-29T00:20:20.730Z",
            "updatedAt": "2025-04-09T20:01:46.080Z"
        },
        "createdAt": "2025-03-29T00:20:20.735Z",
        "updatedAt": "2025-04-08T17:02:25.675Z"
    },
    {
        "id": "<zone-id>",
        "name": "<redacted>",
        "isActive": true,
        "group": {
            "id": "<group-id>",
            "name": "<redacted>",
            "isActive": true,
            "createdAt": "2025-04-08T16:21:44.662Z",
            "updatedAt": "2025-04-08T16:25:01.306Z",
            "systemChangeoverEnabled": true,
            "minRuntime": 30,
            "maxStandby": 60
        },
        "adapter": {
            "id": "<adapter-id>",
            "deviceSerial": "<adapter-serial>",
            "isSimulator": false,
            "roomTemp": 21,
            "spCool": 23.5,
            "spHeat": 21.5,
            "spAuto": null,
            "humidity": null,
            "scheduleOwner": "adapter",
            "power": 1,
            "operationMode": "autoHeat",
            "connected": true,
            "hasSensor": false,
            "hasMhk2": false,
            "timeZone": "America/Los_Angeles",
            "isHeadless": false,
            "lastStatusChangeAt": "2025-04-08T16:20:16.450Z",
            "createdAt": "2025-04-08T16:20:15.982Z",
            "updatedAt": "2025-04-09T18:07:17.220Z"
        },
        "createdAt": "2025-04-08T16:20:15.988Z",
        "updatedAt": "2025-04-08T17:03:27.214Z"
    }
]
```

As of October 2026 each zone also has a `holdMode` object, and its `adapter` has `scheduleHoldEndTime`, `previousOperationMode`, `mhk2DisconnectedAt` and `isIoT`:
```
"holdMode": {
    "id": "<hold-id>",
    "enabled": false,
    "type": "hold",
    "holdType": null,
    "endTime": "2026-03-13T00:03:08.710Z",
    "operationMode": null,
    "fanSpeed": null,
    "airDirection": null,
    "spCool": null,
    "spHeat": null
},
"hasActiveSchedule": false
```

### sites/{site-id}/kumo-station
Description TBD. (I get `"error": "kumoStationNotFound"`, with HTTP 404.) The app adds `?refresh=true` or `?refresh=false`.

### sites/{site-id}/weather
Current weather at the site, passed through from OpenWeatherMap's current-weather API (`coord`, `weather`, `main`, `wind`, `clouds`, `sys`, `name`, `cod`, ...). `main.temp` is in °C.

### sites/{site-id}/dr-programs and sites/{site-id}/program-enroll
Demand-response (utility) programs available to, and enrolled by, the site. Both returned `[]`.

### v4/sites/{site-id}/schedule-seasons
Schedule seasons. The app uses `/v4/` for schedules; the login response's `scheduleVersion` is `"v2"`.
```
[
    {
        "id": "<season-id>",
        "name": "Summer",
        "isRunning": true,
        "isDefault": true,
        "hasSchedules": false,
        "createdAt": "2026-05-05T19:52:03.724Z",
        "updatedAt": "2026-05-05T19:52:03.724Z"
    },
    {
        "id": "<season-id>",
        "name": "Winter",
        "isRunning": false,
        "isDefault": false,
        "hasSchedules": false,
        ...
    }
]
```

### v4/schedule-seasons/{season-id}/schedules
One entry per zone, with its schedule `events` (empty here).
```
[
    {
        "id": "<schedule-id>",
        "zone": {"id": "<zone-id>", "name": "<redacted>"},
        "events": []
    }
]
```

## Group endpoints

**Endpoints**
- /v3/groups/{group-id}

`{group-id}` is the `id` GUID returned from the `/v3/sites/{site-id}/groups` endpoint.

### /v3/groups/{group-id}
```
{
    "id": "<group-id>",
    "name": "<redacted>",
    "isActive": true,
    "createdAt": "2025-04-08T16:21:44.662Z",
    "updatedAt": "2025-04-08T16:25:01.306Z",
    "masterZone": {
        "id": "<zone-id>",
        "name": "<redacted>"
    },
    "systemChangeoverEnabled": true,
    "minRuntime": 30,
    "maxStandby": 30,
    "zones": [
        {
            "id": "<zone-id>",
            "name": "<redacted>",
            "isActive": true,
            "createdAt": "2025-03-29T00:20:20.735Z",
            "updatedAt": "2025-04-08T17:02:25.675Z",
            "isChangeoverPriority": true,
            "changeoverPriority": 1
        },
        {
            "id": "<zone-id>",
            "name": "<redacted>",
            "isActive": true,
            "createdAt": "2025-04-08T16:20:15.988Z",
            "updatedAt": "2025-04-08T17:03:27.214Z",
            "isChangeoverPriority": true,
            "changeoverPriority": 2
        }
    ]
}
```

## Zone endpoints

**Endpoints**
- /v3/zones/{zone-id} (GET, PATCH)
- /v3/zones/{zone-id}/connection-history
- /v3/zones/{zone-id}/notification-preferences
- /v3/zones/{zone-id}/reset-filter (PATCH)

`{zone-id}` is the `id` GUID returned by the `/v3/sites/{site-id}/zones` endpoint

### zones/{zone-id}
```
{
    "id": "<zone-id>",
    "name": "<redacted>",
    "isActive": true,
    "group": {
        "id": "<group-id>",
        "name": "<redacted>",
        "isActive": true,
        "createdAt": "2025-04-08T16:21:44.662Z",
        "updatedAt": "2025-04-09T20:22:09.035Z",
        "systemChangeoverEnabled": true,
        "minRuntime": 30,
        "maxStandby": 60
    },
    "adapter": {
        "id": "<adapter-id>",
        "deviceSerial": "<device-serial>",
        "isSimulator": false,
        "roomTemp": 22,
        "spCool": 23,
        "spHeat": 21.5,
        "spAuto": null,
        "humidity": 43,
        "scheduleOwner": "adapter",
        "power": 1,
        "operationMode": "autoHeat",
        "connected": true,
        "hasSensor": false,
        "hasMhk2": true,
        "timeZone": "America/Los_Angeles",
        "isHeadless": false,
        "lastStatusChangeAt": "2025-04-05T17:41:41.644Z",
        "createdAt": "2025-03-29T00:20:20.730Z",
        "updatedAt": "2025-04-09T20:27:05.796Z"
    },
    "createdAt": "2025-03-29T00:20:20.735Z",
    "updatedAt": "2025-04-08T17:02:25.675Z"
}
```

### PATCH zones/{zone-id}
Renames a zone. The response is the updated zone, as for GET.
```
{"id": "<zone-id>", "name": "<new-name>", "siteId": "<site-id>"}
```

### zones/{zone-id}/connection-history
The adapter's cloud connection history, newest first, paged (`next`, `previous`, `count`, `data`):
```
{
    "next": null,
    "previous": null,
    "count": 20,
    "data": [
        {"start": "2026-10-03T23:20:32.029Z", "end": null, "isConnected": true, "uptime": "18h"},
        {"start": "2026-10-03T01:45:15.927Z", "end": "2026-10-03T23:19:50.906Z", "isConnected": false, "uptime": "22h"},
        ...
    ]
}
```

### zones/{zone-id}/notification-preferences
```
{
    "id": "<preferences-id>",
    "zoneId": "<zone-id>",
    "accountId": "<user-id>",
    "enabled": true,
    "filterDirty": true,
    "zoneError": true,
    "lowTempEnabled": true,
    "lowTemp": 0,
    "highTempEnabled": true,
    "highTemp": 40,
    "sensorSignalLost": true,
    "sensorLowBattery": true,
    "mhk2LowBattery": true,
    "system": true,
    "drEvent": false,
    "filterDirtyReminderInterval": 30,
    "filterDirtyReminderLastSent": "2026-09-14T07:28:55.735Z"
}
```

### PATCH zones/{zone-id}/reset-filter
Clears the dirty-filter indication. No body. The response is `{"preferences": {...}}`, the notification preferences above (without `zoneId` and `accountId`), with `filterDirtyReminderLastSent` set to now.

## Per-device

**Endpoints**
- /v3/devices/{device-serial}
- /v3/devices/{device-serial}/profile
- /v3/devices/{device-serial}/status
- /v3/devices/{device-serial}/initial-settings
- /v3/devices/{device-serial}/kumo-properties
- /v3/devices/{device-serial}/mhk2
- /v3/devices/send-command (POST, see [Commands](#commands))
- /v3/devices/{device-serial}/relay-command (POST, see [Commands](#commands))

These endpoints return information per device (indoor unit).

`{device-serial}` is the `adapter.deviceSerial` field returned by the `/v3/sites/{site-id}/zones` endpoint.
These endpoints return operational data for each indoor unit similar to that returned by the local API.

Importantly, the `status` endpoint returns the `cryptoSerial` value, required for local communication with the indoor unit. (As of October 2026 it no longer does, not even to the official app; see [WebSocket interface](#websocket-interface).)

### devices/{device-serial}
```
{
    "id": "<adapter-id>",
    "deviceSerial": "<device-serial>",
    "rssi": -42,
    "power": 1,
    "operationMode": "autoHeat",
    "humidity": 43,
    "scheduleOwner": "adapter",
    "fanSpeed": "auto",
    "airDirection": "vertical",
    "roomTemp": 22,
    "unusualFigures": 32768,
    "twoFiguresCode": "A0",
    "statusDisplay": 0,
    "spCool": 23,
    "spHeat": 21.5,
    "spAuto": null,
    "runTest": 0,
    "activeThermistor": null,
    "tempSource": null,
    "isSimulator": false,
    "serialNumber": "<redacted>",
    "modelNumber": "SVZ-KP30NA",
    "ledDisabled": false,
    "connected": true,
    "isHeadless": false,
    "lastStatusChangeAt": "2025-04-05T17:41:41.644Z",
    "createdAt": "2025-03-29T00:20:20.730Z",
    "updatedAt": "2025-04-09T20:37:15.239Z",
    "model": {
        "id": "67b6aba8-bc5b-4c92-9ea9-14a5320747c8",
        "brand": "Mitsubishi",
        "material": "SVZ-KP30NA",
        "basicMaterial": "SVZ-KP30NA",
        "replacementMaterial": "SVZ-AP30NL",
        "materialDescription": "MULTI POSITION INDOOR",
        "family": "SVZ",
        "subFamily": "SVZ",
        "materialGroupName": "PAC indoor",
        "serialProfile": "ZEA",
        "materialGroupSeries": "P-Series",
        "isIndoorUnit": true,
        "isDuctless": null,
        "isSwing": null,
        "isPowerfulMode": null,
        "modeDescription": "INDOOR UNIT",
        "isActive": true,
        "frontendAnimation": "ducted",
        "gallery": {
            "id": "fb9153ad-b3ac-4065-bbef-66ceeae809b6",
            "name": "Air handler",
            "imageUrl": "https://dw2p0k56b2hr9.cloudfront.net/small_ME_PVA_Air_Handler_Front_copy_9135d8f255.webp",
            "imageAlt": "Air handler"
        },
        "createdAt": "2025-03-12T19:31:06.038Z",
        "updatedAt": "2025-03-12T19:31:06.038Z"
    },
    "displayConfig": {
        "filter": false,
        "defrost": false,
        "hotAdjust": false,
        "standby": false
    },
    "timeZone": "America/Los_Angeles"
}
```

### devices/{device-serial}/profile
```
[
    {
        "hasModeDry": true,
        "hasModeHeat": true,
        "hasVaneDir": false,
        "hasVaneSwing": false,
        "hasModeVent": true,
        "hasFanSpeedAuto": true,
        "hasInitialSettings": true,
        "hasModeTest": true,
        "numberOfFanSpeeds": 3,
        "extendedTemps": true,
        "usesSetPointInDryMode": true,
        "hasHotAdjust": true,
        "hasDefrost": true,
        "hasStandby": true,
        "maximumSetPoints": {
            "cool": 30,
            "heat": 28,
            "auto": 28
        },
        "minimumSetPoints": {
            "cool": 19,
            "heat": 10,
            "auto": 19
        }
    }
]
```

### devices/{device-serial}/status
```
{
    "autoModeDisable": true,
    "firmwareVersion": "02.06.12",
    "roomTempDisplayOffset": 0,
    "routerSsid": "<redacted>",
    "routerRssi": -42,
    "optimalStart": null,
    "minSetPoint": 16,
    "maxSetPoint": 31,
    "modeHeat": true,
    "modeDry": false,
    "receiverRelay": "MHK2",
    "lastUpdated": "2025-04-09T19:32:36.433Z",
    "cryptoSerial": "<cryptoSerial>",
    "cryptoKeySet": "F"
}
```

As of October 2026, for a ducted unit with no MHK2 on firmware `02.06.26`, the response was only the following. Some of the missing fields (`receiverRelay`, `modeHeat`) may depend on the installation rather than having been removed; `cryptoSerial` and `cryptoKeySet` are gone for every unit seen.
```
{
    "firmwareVersion": "02.06.26",
    "roomTempDisplayOffset": 0,
    "routerSsid": "<redacted>",
    "routerRssi": -51,
    "minSetPoint": 19.5,
    "maxSetPoint": 28,
    "lastUpdated": "2026-10-03T15:18:12.015Z",
    "mac": "<adapter-mac>"
}
```

### devices/{device-serial}/initial-settings
```
[
    {
        "deviceSerial": "<redacted>",
        "settingNumber": 1,
        "settingValue": 2
    },
    {
        "deviceSerial": "<redacted>",
        "settingNumber": 2,
        "settingValue": 1
    },
    ...
]
```
### devices/{device-serial}/kumo-properties
```
{
    "deviceSerial": "<device-serial>",
    "reporting": {},
    "heatModeDisable": false,
    "connected": false,
    "outdoorAirTemperature": null,
    "sourceReport": null,
    "lastUpdated": "2025-04-09T20:24:45.093Z"
}
```

### devices/{device-serial}/mhk2
The MHK2 wireless controller paired with the unit. Without one, HTTP 404 and `{"error": "mhk2NotFound"}`.

## Commands

The Comfort app (October 2026) controls units through the cloud, not through their local API. Commands are POSTed with the access token, and the resulting state arrives as Socket.IO `device_update` events (see [`realValues`](#observed-in-a-capture-of-the-comfort-app-october-2026)).

### devices/send-command
Changes a unit's operating state.
```
{
    "deviceSerial": "<device-serial>",
    "deviceCommands": {
        "<device-serial>": {"operationMode": "dry", "spCool": 24.5, "spHeat": 24}
    }
}
```
Response:
```
{"devices": ["<device-serial>"]}
```
Fields seen in `deviceCommands`: `operationMode` (`dry`, `off`), `fanSpeed` (`quiet`, `low`, `powerful`, `auto`), `spCool`, `spHeat`. The app sent the setpoints along with the switch to `dry`, and `operationMode: "off"` alone to turn the unit off. The mode values are presumably the same as `operationMode` in `device_update` (which also reports `cool` and `vent`). `deviceCommands` is keyed by serial, which suggests one call can command several units; only single-unit calls have been seen.

### devices/{device-serial}/relay-command
Changes adapter settings. Seen toggling the adapter LED:
```
{"serial": "<device-serial>", "adapter": {"status": {"ledDisabled": true}}}
```
The response echoes the command without `serial`:
```
{"adapter": {"status": {"ledDisabled": true}}}
```
The body after `serial` has the shape of the adapter's local API commands (the contents of the local API's `"c"` object; pykumo reads the same `adapter.status` object locally). It may relay other local API commands to the adapter, which would allow local-API-equivalent control without the adapter password or `cryptoSerial`. This is untested beyond `ledDisabled`.

## Errors

Errors come back as `{"error": "<code>"}`. Codes seen:

| HTTP | `error` | When |
|---|---|---|
| 401 | `notAuthorized` | Expired access token. The app then calls `/v3/refresh` and retries. |
| 401 | `notAuthToken` | No usable token, e.g. after `/v3/logout`. |
| 404 | `kumoStationNotFound` | `sites/{site-id}/kumo-station` on a site without one. |
| 404 | `mhk2NotFound` | `devices/{device-serial}/mhk2` on a unit without one. |
