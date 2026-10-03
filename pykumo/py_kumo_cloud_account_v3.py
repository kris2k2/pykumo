"""Kumo Cloud V3 API client for retrieving device credentials.

The Mitsubishi Comfort app (successor to Kumo Cloud) uses a V3 REST + WebSocket
API at app-prod.kumocloud.com. This module retrieves the credentials needed for
local control of indoor units:
  - password (from WebSocket adapter_update events)
  - cryptoSerial (from /v3/devices/{serial}/status REST endpoint)

These are merged into existing pykumo data structures so the local API code
(PyKumoBase._token / ._request) works without changes.
"""

import base64
import json
import logging
import time
import threading
from typing import Optional

import requests
from requests.exceptions import Timeout

from . import traffic

_LOGGER = logging.getLogger(__name__)

V3_BASE_URL = "https://app-prod.kumocloud.com"
SOCKET_URL = "https://socket-prod.kumocloud.com"
V3_APP_VERSION = "3.2.4"
V3_CLOUD_TIMEOUT = (10, 30)  # (connect, read)

V3_BASE_HEADERS = {
    "Accept": "application/json",
    "Accept-Encoding": "gzip, deflate, br",
    "x-app-version": V3_APP_VERSION,
    "Content-Type": "application/json",
}

# Engine.IO / Socket.IO protocol message prefixes
EIO_PING = "2"
EIO_PONG = "3"
SIO_CONNECT = "40"
SIO_EVENT = "42"
SIO_CONNECT_ERROR = "44"


def _elapsed_ms(resp):
    elapsed = getattr(resp, "elapsed", None)
    return round(elapsed.total_seconds() * 1000) if elapsed is not None else None


def _log_http(method: str, path: str, request_body=None, resp=None, error=None):
    """Report one cloud REST exchange to the traffic log."""
    if not traffic.is_enabled():
        return
    traffic.log_event("cloud", "send", method=method, path=path, body=request_body)
    if error is not None:
        traffic.log_event(
            "cloud",
            "error",
            method=method,
            path=path,
            error=f"{type(error).__name__}: {error}",
        )
    elif resp is not None:
        traffic.log_event(
            "cloud",
            "recv",
            method=method,
            path=path,
            status=resp.status_code,
            elapsed_ms=_elapsed_ms(resp),
            body=traffic.decode_body(resp.text),
        )


def _log_socketio(direction: str, raw, **fields):
    """Report Socket.IO frames sent or received to the traffic log."""
    if not traffic.is_enabled():
        return
    traffic.log_event(
        "socketio",
        direction,
        packets=traffic.decode_socketio_payload(raw),
        **fields,
    )


class KumoCloudV3:
    """Client for the Kumo Cloud V3 API used by the Comfort app."""

    def __init__(self, username: str, password: str):
        self._username = username
        self._password = password
        self._access_token: Optional[str] = None
        self._refresh_token: Optional[str] = None
        self._cancel_event = threading.Event()

    # ── Authentication ──────────────────────────────────────

    def login(self) -> bool:
        """Authenticate and obtain JWT tokens."""
        body = {
            "username": self._username,
            "password": self._password,
            "appVersion": V3_APP_VERSION,
        }
        try:
            resp = requests.post(
                f"{V3_BASE_URL}/v3/login",
                headers=V3_BASE_HEADERS,
                json=body,
                timeout=V3_CLOUD_TIMEOUT,
            )
        except Exception as ex:
            _log_http("POST", "/v3/login", body, error=ex)
            _LOGGER.warning("V3 login error: %s", ex)
            return False
        _log_http("POST", "/v3/login", body, resp=resp)

        if not resp.ok:
            _LOGGER.warning("V3 login failed: %s %s", resp.status_code, resp.text[:200])
            return False

        token_data = resp.json().get("token", {})
        self._access_token = token_data.get("access")
        self._refresh_token = token_data.get("refresh")

        if not self._access_token:
            _LOGGER.warning("V3 login response missing access token")
            return False

        _LOGGER.info("V3 login successful")
        return True

    def refresh(self) -> bool:
        """Refresh the access token."""
        if not self._refresh_token:
            return False

        headers = {**V3_BASE_HEADERS, "Authorization": f"Bearer {self._refresh_token}"}
        try:
            resp = requests.post(
                f"{V3_BASE_URL}/v3/refresh",
                headers=headers,
                json={"refresh": self._refresh_token},
                timeout=V3_CLOUD_TIMEOUT,
            )
        except Exception as ex:
            _log_http("POST", "/v3/refresh", {"refresh": self._refresh_token}, error=ex)
            _LOGGER.warning("V3 token refresh error: %s", ex)
            return False
        _log_http("POST", "/v3/refresh", {"refresh": self._refresh_token}, resp=resp)

        if not resp.ok:
            return False

        data = resp.json()
        self._access_token = data.get("access")
        self._refresh_token = data.get("refresh")
        return bool(self._access_token)

    def _auth_headers(self) -> dict:
        return {**V3_BASE_HEADERS, "Authorization": f"Bearer {self._access_token}"}

    def _get_user_id_from_token(self) -> Optional[str]:
        """Extract user ID from the JWT payload (needed for account subscription)."""
        if not self._access_token:
            return None
        try:
            payload_b64 = self._access_token.split(".")[1]
            # Add base64 padding (0 when already aligned, 1-3 otherwise)
            payload_b64 += "=" * ((-len(payload_b64)) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_b64))
            user_id = payload.get("id")
            return str(user_id) if user_id is not None else None
        except Exception as ex:
            _LOGGER.warning("Failed to extract user ID from JWT: %s", ex)
            return None

    # ── REST API ────────────────────────────────────────────

    def _get(self, path: str):
        """Authenticated GET with automatic token refresh on 401."""
        url = f"{V3_BASE_URL}{path}"
        try:
            resp = requests.get(
                url, headers=self._auth_headers(), timeout=V3_CLOUD_TIMEOUT
            )
        except Exception as ex:
            _log_http("GET", path, error=ex)
            _LOGGER.warning("V3 GET %s error: %s", path, ex)
            return None
        _log_http("GET", path, resp=resp)

        if resp.status_code == 401 and self.refresh():
            try:
                resp = requests.get(
                    url, headers=self._auth_headers(), timeout=V3_CLOUD_TIMEOUT
                )
            except Exception as ex:
                _log_http("GET", path, error=ex)
                _LOGGER.warning("V3 GET %s error after refresh: %s", path, ex)
                return None
            _log_http("GET", path, resp=resp)

        if not resp.ok:
            _LOGGER.warning("V3 GET %s: HTTP %s", path, resp.status_code)
            return None

        try:
            return resp.json()
        except Exception:
            return None

    def get_sites(self) -> list:
        result = self._get("/v3/sites/")
        return result if isinstance(result, list) else []

    def get_zones(self, site_id: str) -> list:
        result = self._get(f"/v3/sites/{site_id}/zones")
        return result if isinstance(result, list) else []

    def get_device_status(self, serial: str) -> Optional[dict]:
        return self._get(f"/v3/devices/{serial}/status")

    def cancel(self):
        """Signal the Socket.IO poll loop to stop (for clean HA shutdown)."""
        self._cancel_event.set()

    # ── Socket.IO Password Retrieval ────────────────────────

    def get_passwords_via_websocket(
        self, device_serials: list, timeout_secs: int = 30
    ) -> dict:
        """Connect to Socket.IO and collect adapter_update events with passwords.

        Returns once every device has sent an adapter_update, whether or not
        it carried a password, or after timeout_secs.
        """
        if not self._access_token:
            return {}

        self._cancel_event.clear()
        try:
            return self._socketio_session(set(device_serials), timeout_secs)
        except Exception as ex:
            _LOGGER.warning("WebSocket password retrieval error: %s", ex)
            return {}

    def _socketio_session(
        self, serials_needed: set, timeout_secs: int, _retried: bool = False
    ) -> dict:
        """Run a complete Socket.IO session: handshake, subscribe, poll.

        If the namespace connect is rejected (expired token), refreshes
        the token and retries once.
        """
        passwords = {}
        session = requests.Session()
        try:
            return self._socketio_poll(
                session, serials_needed, timeout_secs, passwords, _retried
            )
        finally:
            session.close()

    def _socketio_poll(
        self,
        session,
        serials_needed: set,
        timeout_secs: int,
        passwords: dict,
        _retried: bool,
    ) -> dict:
        """Inner Socket.IO poll loop (session is managed by caller)."""
        base_params = {"EIO": "4", "transport": "polling"}
        headers = {"Authorization": f"Bearer {self._access_token}", "Accept": "*/*"}

        # 1. Handshake
        try:
            resp = session.get(
                f"{SOCKET_URL}/socket.io/",
                params=base_params,
                headers=headers,
                timeout=15,
            )
        except Exception as ex:
            traffic.log_event(
                "socketio",
                "error",
                stage="handshake",
                error=f"{type(ex).__name__}: {ex}",
            )
            _LOGGER.warning("Socket.IO handshake failed: %s", ex)
            return passwords
        _log_socketio("recv", resp.text, stage="handshake", status=resp.status_code)

        if not resp.ok or not resp.text.startswith("0"):
            _LOGGER.warning("Socket.IO handshake unexpected: %s", resp.text[:200])
            return passwords

        try:
            handshake = json.loads(resp.text[1:])
            sid = handshake.get("sid")
        except json.JSONDecodeError:
            _LOGGER.warning("Socket.IO handshake parse error")
            return passwords
        # The server pings every pingInterval ms, so a long poll can take
        # that long to answer when nothing else happens; wait a bit longer.
        try:
            long_poll_timeout = float(handshake.get("pingInterval")) / 1000 + 5
        except (TypeError, ValueError):
            long_poll_timeout = 30

        _LOGGER.debug("Socket.IO connected, sid=%s", sid)
        poll_params = {**base_params, "sid": sid}
        post_headers = {**headers, "Content-Type": "text/plain;charset=UTF-8"}

        def _post(data):
            _log_socketio("send", data, sid=sid)
            session.post(
                f"{SOCKET_URL}/socket.io/",
                params=poll_params,
                headers=post_headers,
                data=data,
                timeout=10,
            )

        def _poll(timeout=10):
            try:
                resp = session.get(
                    f"{SOCKET_URL}/socket.io/",
                    params=poll_params,
                    headers=headers,
                    timeout=timeout,
                )
            except Exception as ex:
                traffic.log_event(
                    "socketio",
                    "error",
                    sid=sid,
                    error=f"{type(ex).__name__}: {ex}",
                )
                raise
            _log_socketio("recv", resp.text, sid=sid, status=resp.status_code)
            return resp

        # 2. Namespace connect
        _post(SIO_CONNECT)
        resp = _poll()

        # Check for rejection (token expired)
        if resp.ok and resp.text.startswith(SIO_CONNECT_ERROR) and not _retried:
            _LOGGER.info("Socket.IO namespace rejected, refreshing token...")
            if self.refresh():
                return self._socketio_session(
                    serials_needed, timeout_secs, _retried=True
                )
            _LOGGER.warning("Token refresh failed")
            return passwords

        # 3. Account-level subscribe (required for adapter_update events)
        user_id = self._get_user_id_from_token()
        if user_id:
            _post(f'{SIO_EVENT}["subscribe","","{user_id}"]')
            resp = _poll()
            if resp.ok:
                self._extract_passwords(resp.text, passwords, serials_needed)
        else:
            _LOGGER.warning(
                "Could not extract user ID — adapter_update events may not arrive"
            )

        # 4. Subscribe to each device
        _post("\x1e".join(f'{SIO_EVENT}["subscribe","{s}"]' for s in serials_needed))
        resp = _poll()
        if resp.ok:
            self._extract_passwords(resp.text, passwords, serials_needed)

        # 5. Force adapter_update events (contains passwords)
        _post(
            "\x1e".join(
                f'{SIO_EVENT}["force_adapter_request","{s}","adapterStatus"]'
                for s in serials_needed
            )
        )

        # 6. Send device_status_v2 to trigger updates
        status_msgs = [f'{SIO_EVENT}["device_status_v2",""]']
        status_msgs.extend(
            f'{SIO_EVENT}["device_status_v2","{s}"]' for s in serials_needed
        )
        _post("\x1e".join(status_msgs))

        # 7. Poll for adapter_update events. The cloud may leave the password
        # out of the reply to force_adapter_request, and asking again won't
        # change that, so stop waiting for a device once it has answered.
        answered = set()
        deadline = time.monotonic() + timeout_secs
        poll_count = 0
        while time.monotonic() < deadline:
            if not serials_needed - answered - set(passwords):
                break
            if self._cancel_event.is_set():
                break

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break

            poll_count += 1
            try:
                resp = _poll(timeout=min(long_poll_timeout, remaining + 1))
            except Timeout:
                continue
            except Exception:
                break

            if not resp.ok:
                break

            self._extract_passwords(resp.text, passwords, serials_needed, answered)

            # Respond to ping with pong
            if EIO_PING in self._split_messages(resp.text):
                try:
                    _post(EIO_PONG)
                except Exception:
                    pass

        _LOGGER.info(
            "Socket.IO: found passwords for %d/%d devices (%d polls)",
            len(passwords),
            len(serials_needed),
            poll_count,
        )
        for serial in sorted(answered - set(passwords)):
            _LOGGER.info(
                "Kumo Cloud sent adapter status for %s without its password",
                serial,
            )
        return passwords

    @staticmethod
    def _split_messages(raw: str) -> list:
        """Split EIO4 batched messages (separated by \\x1e)."""
        if not raw:
            return []
        return raw.split("\x1e") if "\x1e" in raw else [raw]

    def _extract_passwords(
        self, raw: str, passwords: dict, serials_needed: set, answered=None
    ):
        """Parse Socket.IO messages for adapter_update events with passwords.

        Serials whose adapter_update arrived, with or without a password, are
        added to answered if given.
        """
        for msg in self._split_messages(raw):
            # Strip Engine.IO length prefix if present
            if ":" in msg and msg.split(":")[0].isdigit():
                msg = msg.split(":", 1)[1]

            if not msg.startswith(SIO_EVENT):
                continue

            try:
                payload = json.loads(msg[len(SIO_EVENT) :])
            except (json.JSONDecodeError, IndexError):
                continue

            if (
                isinstance(payload, list)
                and len(payload) >= 2
                and payload[0] == "adapter_update"
                and isinstance(payload[1], dict)
            ):
                serial = payload[1].get("deviceSerial", "")
                password = payload[1].get("password", "")
                if serial in serials_needed and answered is not None:
                    answered.add(serial)
                if serial in serials_needed and password:
                    passwords[serial] = password
                    _LOGGER.info("Got password for %s via adapter_update", serial)

    # ── High-Level Credential Retrieval ─────────────────────

    def get_all_device_credentials(self) -> dict:
        """Retrieve credentials for all devices on this account.

        Returns dict: serial -> {password, cryptoSerial, label, unitType, mac}
        """
        if not self._access_token and not self.login():
            _LOGGER.warning("V3 login failed")
            return {}

        # Discover devices from sites/zones
        devices = {}
        for site in self.get_sites():
            site_id = site.get("id")
            if not site_id:
                continue
            for zone in self.get_zones(site_id):
                adapter = zone.get("adapter", {})
                serial = adapter.get("deviceSerial", "")
                if serial:
                    devices[serial] = {
                        "label": zone.get("name", ""),
                        "unitType": adapter.get("unitType", "ductless"),
                        "mac": adapter.get("macAddress", ""),
                        "serial": serial,
                        "password": "",
                        "cryptoSerial": "",
                    }

        if not devices:
            _LOGGER.warning("No devices found via V3 API")
            return {}

        _LOGGER.info("V3 API: found %d devices", len(devices))

        # Get cryptoSerials from status endpoint
        for serial in devices:
            status = self.get_device_status(serial)
            if isinstance(status, dict):
                crypto = status.get("cryptoSerial", "")
                if crypto:
                    devices[serial]["cryptoSerial"] = crypto

        # Get passwords via Socket.IO
        passwords = self.get_passwords_via_websocket(
            list(devices.keys()), timeout_secs=60
        )
        for serial, password in passwords.items():
            if serial in devices:
                devices[serial]["password"] = password

        has_crypto = sum(1 for d in devices.values() if d.get("cryptoSerial"))
        has_pw = sum(1 for d in devices.values() if d.get("password"))
        _LOGGER.info(
            "V3 credentials: %d/%d cryptoSerial, %d/%d password",
            has_crypto,
            len(devices),
            has_pw,
            len(devices),
        )

        return devices
