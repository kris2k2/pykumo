"""Query indoor-unit adapters through Kumo Cloud's relay-command endpoint.

Kumo Cloud no longer hands out the adapter password and cryptoSerial that
the local API needs, but the Comfort app changes adapter settings by POSTing
local-API commands to /v3/devices/{serial}/relay-command. This tool sends
local-API queries the same way, to see whether the adapter's answers come
back, and whether any of them carry the credentials.

    python -m pykumo.relay_probe --username you@example.com

The password is read from KUMO_PASSWORD, or prompted for. By default every
unit on the account is sent a few read-only queries (empty objects, which
the local API answers with the current values). Credential-like fields are
masked in the output unless --show-secrets is given.
"""

import argparse
import base64
import binascii
import getpass
import json
import logging
import os
import re
import sys

from . import traffic
from .py_kumo_cloud_account_v3 import KumoCloudV3
from .py_kumo_discovery import probe_ip

DEFAULT_QUERIES = (
    {"adapter": {"status": {}}},
    {"adapter": {"info": {}}},
    {"indoorUnit": {"status": {}}},
    {"indoorUnit": {"profile": {}}},
)

# Field names that may hold credentials.
_SECRET_RE = re.compile(r"crypto|password|passwd|secret|token|key", re.IGNORECASE)
_MASK = "**MASKED**"


def is_read_only(query) -> bool:
    """Return True if every leaf of a local-API query is an empty object.

    The local API answers an empty object with the current values; anything
    else sets a value.
    """
    if not isinstance(query, dict):
        return False
    return all(
        isinstance(v, dict) and (not v or is_read_only(v)) for v in query.values()
    )


def parse_query(text: str) -> dict:
    """Parse a query given on the command line, with or without its "c"."""
    query = json.loads(text)
    if not isinstance(query, dict):
        raise ValueError("a query must be a JSON object")
    if set(query) == {"c"} and isinstance(query["c"], dict):
        query = query["c"]
    return query


def find_secrets(value, path=()):
    """Yield (path, value) for every credential-like field in value."""
    if isinstance(value, dict):
        for key, item in value.items():
            here = path + (str(key),)
            if _SECRET_RE.search(str(key)) and not isinstance(item, (dict, list)):
                yield here, item
            else:
                yield from find_secrets(item, here)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from find_secrets(item, path + (str(index),))


def mask_secrets(value):
    """Return a copy of value with credential-like fields masked."""
    if isinstance(value, dict):
        return {
            key: (
                _MASK
                if _SECRET_RE.search(str(key))
                and item not in (None, "")
                and not isinstance(item, (dict, list))
                else mask_secrets(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [mask_secrets(item) for item in value]
    return value


def credentials_from(secrets: dict):
    """Pick a password and cryptoSerial out of the fields found, if both are.

    Returns the local API's credentials dict (password as bytes,
    crypto_serial as a bytearray) or None.
    """
    password = crypto = None
    for path, value in secrets.items():
        name = path[-1].lower()
        if name == "password" and isinstance(value, str) and value:
            password = value
        elif name == "cryptoserial" and isinstance(value, str) and value:
            crypto = value
    if password is None or crypto is None:
        return None
    try:
        crypto_serial = bytearray.fromhex(crypto)
    except ValueError:
        return None
    try:
        # Kumo Cloud used to hand out the password base64-encoded.
        password_bytes = base64.b64decode(password, validate=True)
    except (binascii.Error, ValueError):
        password_bytes = password.encode("utf-8")
    return {"password": password_bytes, "crypto_serial": crypto_serial}


def list_units(v3: KumoCloudV3) -> dict:
    """Return {serial: name} for every unit on the account."""
    units = {}
    for site in v3.get_sites():
        if not site.get("id"):
            continue
        for zone in v3.get_zones(site["id"]):
            serial = (zone.get("adapter") or {}).get("deviceSerial")
            if serial:
                units[serial] = zone.get("name", "")
    return units


def _print_json(value, show_secrets: bool) -> None:
    shown = value if show_secrets else mask_secrets(value)
    print(json.dumps(shown, indent=2, sort_keys=True, ensure_ascii=False))


def probe_unit(v3, serial, name, queries, show_secrets, verify_address=None):
    """Send each query to one unit through the relay and report what came
    back. Returns the credential-like fields found, as {path: value}.
    """
    print(f"=== {name or '(unnamed)'} ({serial})")
    found = {}
    for query in queries:
        print(f"\n--> relay-command {json.dumps(query)}")
        response = v3.relay_command(serial, query)
        if response is None:
            print("<-- no usable answer (see the warning above)")
            continue
        print("<--")
        _print_json(response, show_secrets)
        if response == query:
            print("    (an echo of the query, not the adapter's answer)")
        for path, value in find_secrets(response):
            found[path] = value

    print()
    if not found:
        print("No credential-like fields in any answer.")
        return found
    print("Credential-like fields found:")
    for path, value in found.items():
        shown = value if show_secrets else _MASK
        print(f"  {'.'.join(path)} = {shown}")

    creds = credentials_from(found)
    if creds is None:
        print("No password and cryptoSerial pair to build local credentials from.")
    elif verify_address:
        ok = probe_ip(verify_address, creds, timeout=3.0)
        print(
            f"Local API at {verify_address} "
            + ("ACCEPTED these credentials." if ok else "did not accept them.")
        )
    else:
        print("Found a password and cryptoSerial; --verify-address checks them.")
    return found


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m pykumo.relay_probe",
        description=(
            "Send local-API queries to Kumo adapters through Kumo Cloud's "
            "relay-command endpoint and look for credentials in the answers."
        ),
    )
    parser.add_argument(
        "--username",
        default=os.environ.get("KUMO_USERNAME"),
        help="Kumo Cloud account (default: $KUMO_USERNAME)",
    )
    parser.add_argument(
        "--serial",
        action="append",
        help="unit serial to query; repeat for several (default: every unit)",
    )
    parser.add_argument(
        "--query",
        action="append",
        type=parse_query,
        help=(
            'local-API query as JSON, with or without its "c" wrapper, e.g. '
            '\'{"adapter":{"status":{}}}\'; repeat for several '
            "(default: adapter status and info, indoor unit status and profile)"
        ),
    )
    parser.add_argument(
        "--allow-writes",
        action="store_true",
        help="allow queries that set values rather than read them",
    )
    parser.add_argument(
        "--show-secrets",
        action="store_true",
        help="print credential-like fields in the clear",
    )
    parser.add_argument(
        "--verify-address",
        metavar="IP",
        help="if a password and cryptoSerial turn up, try them on this adapter",
    )
    parser.add_argument(
        "--traffic",
        action="store_true",
        help="log every cloud exchange to stderr (pykumo.traffic, JSON lines)",
    )
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.username:
        parser.error("--username or $KUMO_USERNAME is required")
    queries = args.query or list(DEFAULT_QUERIES)
    writes = [q for q in queries if not is_read_only(q)]
    if writes and not args.allow_writes:
        parser.error(
            "these queries set values: "
            + ", ".join(json.dumps(q) for q in writes)
            + " (add --allow-writes to send them anyway)"
        )
    if args.verify_address and len(args.serial or []) != 1:
        parser.error("--verify-address needs exactly one --serial")

    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    if args.traffic:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(message)s"))
        traffic_logger = logging.getLogger(traffic.TRAFFIC_LOGGER_NAME)
        traffic_logger.addHandler(handler)
        traffic_logger.setLevel(logging.DEBUG)
        traffic_logger.propagate = False
        traffic.set_redact_secrets(not args.show_secrets)

    password = os.environ.get("KUMO_PASSWORD") or getpass.getpass(
        f"Kumo Cloud password for {args.username}: "
    )
    v3 = KumoCloudV3(args.username, password)
    if not v3.login():
        print("Kumo Cloud login failed.", file=sys.stderr)
        return 1

    units = list_units(v3)
    serials = args.serial or list(units)
    if not serials:
        print("No units found on this account.", file=sys.stderr)
        return 1

    for serial in serials:
        probe_unit(
            v3,
            serial,
            units.get(serial, ""),
            queries,
            args.show_secrets,
            args.verify_address,
        )
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
