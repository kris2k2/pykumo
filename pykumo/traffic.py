"""Optional capture of raw API traffic for protocol discovery.

Every exchange pykumo has with an indoor-unit adapter, the Kumo Cloud V3
REST API, or the Kumo Cloud Socket.IO channel is reported to the
``pykumo.traffic`` logger as one DEBUG record per event. Nothing is emitted
unless that logger is enabled for DEBUG, so the cost when unused is a single
level check per exchange.

Each record's message is a single-line JSON object, and the same object is
attached to the record as ``record.kumo_traffic`` for handlers that want the
structured form. Fields common to every event:

  ts         wall-clock time (ISO 8601, UTC)
  channel    "local" (adapter /api), "cloud" (V3 REST) or "socketio"
  direction  "send", "recv" or "error"

Credentials, tokens and similar values are replaced with REDACTED before the
event is logged; see set_redact_secrets().

To capture traffic to a file::

    import logging
    handler = logging.FileHandler("kumo_traffic.jsonl")
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("pykumo.traffic")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
"""

import json
import logging
from datetime import datetime, timezone

TRAFFIC_LOGGER_NAME = "pykumo.traffic"
REDACTED = "**REDACTED**"

# Keys whose values are credentials or personal data. Matched
# case-insensitively against dict keys anywhere in a logged payload.
SECRET_KEYS = frozenset(
    {
        "access",
        "authorization",
        "cryptoserial",
        "crypto_serial",
        "email",
        "firstname",
        "lastname",
        "password",
        "phone",
        "refresh",
        "token",
        "username",
    }
)

_LOGGER = logging.getLogger(TRAFFIC_LOGGER_NAME)
_redact = True


def set_redact_secrets(enabled: bool) -> None:
    """Choose whether credentials and tokens are masked in captured traffic.

    Redaction is on by default. Turning it off writes adapter passwords,
    cryptoSerials and cloud tokens to the log in the clear.
    """
    global _redact  # pylint: disable=global-statement
    _redact = bool(enabled)


def redact_secrets_enabled() -> bool:
    """Return True if secrets are masked in captured traffic."""
    return _redact


def is_enabled() -> bool:
    """Return True if traffic events are currently being captured."""
    return _LOGGER.isEnabledFor(logging.DEBUG)


def redact(value):
    """Return a copy of value with secret fields masked (if enabled)."""
    if not _redact:
        return value
    if isinstance(value, dict):
        return {
            k: (
                REDACTED
                if isinstance(k, str) and k.lower() in SECRET_KEYS and v
                else redact(v)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def decode_body(body):
    """Best-effort conversion of a request/response body to loggable data.

    JSON bodies are parsed so they can be redacted field by field; anything
    else is logged as text.
    """
    if body is None:
        return None
    if isinstance(body, (bytes, bytearray)):
        body = body.decode("utf-8", errors="replace")
    if isinstance(body, str):
        try:
            return json.loads(body)
        except ValueError:
            return body
    return body


def log_event(channel: str, direction: str, **fields) -> None:
    """Record one traffic event, if capture is enabled."""
    if not _LOGGER.isEnabledFor(logging.DEBUG):
        return
    event = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "channel": channel,
        "direction": direction,
    }
    event.update(redact(fields))
    try:
        message = json.dumps(event, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        message = repr(event)
    _LOGGER.debug("%s", message, extra={"kumo_traffic": event})


_EIO_TYPES = {
    "0": "open",
    "1": "close",
    "2": "ping",
    "3": "pong",
    "4": "message",
    "5": "upgrade",
    "6": "noop",
}
_SIO_TYPES = {
    "0": "connect",
    "1": "disconnect",
    "2": "event",
    "3": "ack",
    "4": "connect_error",
    "5": "binary_event",
    "6": "binary_ack",
}


def decode_socketio_payload(raw) -> list:
    """Split an Engine.IO v4 polling payload into readable packets.

    Each packet becomes a dict with the Engine.IO type and, for messages,
    the Socket.IO type plus the event name and arguments. Anything that
    does not parse is kept as raw text so nothing is lost.
    """
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if not raw:
        return []
    packets = []
    for msg in raw.split("\x1e"):
        # Tolerate an Engine.IO v3 style "<length>:" prefix.
        head, sep, rest = msg.partition(":")
        if sep and head.isdigit():
            msg = rest
        if not msg:
            continue
        packet = {"eio": _EIO_TYPES.get(msg[0], msg[0])}
        body = msg[1:]
        if msg[0] == "4" and body:
            packet["sio"] = _SIO_TYPES.get(body[0], body[0])
            body = body[1:]
            # Optional namespace ("/nsp,") and ack id (digits) precede the data.
            if body.startswith("/"):
                nsp, _, body = body.partition(",")
                packet["namespace"] = nsp
            ack = ""
            while body[:1].isdigit():
                ack, body = ack + body[0], body[1:]
            if ack:
                packet["ack_id"] = int(ack)
        if body:
            data = decode_body(body)
            if (
                packet.get("sio") == "event"
                and isinstance(data, list)
                and data
                and isinstance(data[0], str)
            ):
                packet["event"] = data[0]
                packet["args"] = data[1:]
            else:
                packet["data"] = data
        packets.append(packet)
    return packets
