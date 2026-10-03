"""Tests for the pykumo.traffic capture logger."""

import json
import logging
import unittest
from unittest.mock import MagicMock, patch

from pykumo import traffic
from pykumo.py_kumo_cloud_account_v3 import KumoCloudV3
from pykumo.py_kumo_discovery import probe_ip
from tests.test_connection_handling import _CFG, _FakeAdapter
from pykumo import PyKumo


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.events = []
        self.messages = []

    def emit(self, record):
        self.events.append(record.kumo_traffic)
        self.messages.append(record.getMessage())


class _TrafficTestCase(unittest.TestCase):
    def setUp(self):
        self.capture = _Capture()
        logger = logging.getLogger(traffic.TRAFFIC_LOGGER_NAME)
        self._old_level = logger.level
        self._old_propagate = logger.propagate
        logger.addHandler(self.capture)
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        traffic.set_redact_secrets(True)

    def tearDown(self):
        logger = logging.getLogger(traffic.TRAFFIC_LOGGER_NAME)
        logger.removeHandler(self.capture)
        logger.setLevel(self._old_level)
        logger.propagate = self._old_propagate
        traffic.set_redact_secrets(True)


class TestTrafficHelpers(_TrafficTestCase):
    def test_disabled_logger_emits_nothing(self):
        logging.getLogger(traffic.TRAFFIC_LOGGER_NAME).setLevel(logging.INFO)
        self.assertFalse(traffic.is_enabled())
        traffic.log_event("local", "send", body={"c": {}})
        self.assertEqual(self.capture.events, [])

    def test_message_is_single_line_json(self):
        traffic.log_event("local", "send", body={"c": {"indoorUnit": {}}})
        self.assertEqual(len(self.capture.messages), 1)
        message = self.capture.messages[0]
        self.assertNotIn("\n", message)
        parsed = json.loads(message)
        self.assertEqual(parsed["channel"], "local")
        self.assertEqual(parsed["direction"], "send")
        self.assertEqual(parsed["body"], {"c": {"indoorUnit": {}}})
        self.assertIn("ts", parsed)

    def test_secrets_redacted_at_any_depth(self):
        traffic.log_event(
            "cloud",
            "recv",
            body={
                "token": {"access": "a", "refresh": "r"},
                "devices": [{"serial": "S1", "Password": "pw", "cryptoSerial": "c"}],
                "username": "u@example.com",
            },
        )
        body = self.capture.events[0]["body"]
        self.assertEqual(body["token"], traffic.REDACTED)
        self.assertEqual(body["username"], traffic.REDACTED)
        device = body["devices"][0]
        self.assertEqual(device["serial"], "S1")
        self.assertEqual(device["Password"], traffic.REDACTED)
        self.assertEqual(device["cryptoSerial"], traffic.REDACTED)

    def test_empty_secret_values_are_left_visible(self):
        traffic.log_event("cloud", "recv", body={"password": ""})
        self.assertEqual(self.capture.events[0]["body"]["password"], "")

    def test_redaction_can_be_disabled(self):
        traffic.set_redact_secrets(False)
        traffic.log_event("cloud", "recv", body={"password": "pw"})
        self.assertEqual(self.capture.events[0]["body"]["password"], "pw")

    def test_decode_body(self):
        self.assertEqual(traffic.decode_body(b'{"a": 1}'), {"a": 1})
        self.assertEqual(traffic.decode_body("not json"), "not json")
        self.assertIsNone(traffic.decode_body(None))

    def test_decode_socketio_payload(self):
        raw = (
            '0{"sid":"abc"}\x1e2\x1e40\x1e'
            '42["adapter_update",{"deviceSerial":"S1","password":"pw"}]\x1e'
            '4312["ack",1]\x1e44{"message":"unauthorized"}'
        )
        packets = traffic.decode_socketio_payload(raw)
        self.assertEqual(
            packets[0], {"eio": "open", "data": {"sid": "abc"}}
        )  # handshake
        self.assertEqual(packets[1], {"eio": "ping"})
        self.assertEqual(packets[2], {"eio": "message", "sio": "connect"})
        self.assertEqual(packets[3]["event"], "adapter_update")
        self.assertEqual(packets[3]["args"], [{"deviceSerial": "S1", "password": "pw"}])
        self.assertEqual(packets[4]["sio"], "ack")
        self.assertEqual(packets[4]["ack_id"], 12)
        self.assertEqual(packets[4]["data"], ["ack", 1])
        self.assertEqual(packets[5]["sio"], "connect_error")


class TestLocalTraffic(_TrafficTestCase):
    def setUp(self):
        super().setUp()
        self.adapter = _FakeAdapter()
        self.addCleanup(self.adapter.close)

    def test_adapter_request_and_response_captured(self):
        unit = PyKumo("Test Unit", self.adapter.address, _CFG, timeouts=(1.0, 1.0))
        unit._request(b'{"c":{"indoorUnit":{"status":{}}}}')
        sends = [e for e in self.capture.events if e["direction"] == "send"]
        recvs = [e for e in self.capture.events if e["direction"] == "recv"]
        self.assertEqual(len(sends), 1)
        self.assertEqual(len(recvs), 1)
        self.assertEqual(sends[0]["channel"], "local")
        self.assertEqual(sends[0]["unit"], "Test Unit")
        self.assertEqual(sends[0]["body"], {"c": {"indoorUnit": {"status": {}}}})
        self.assertEqual(recvs[0]["status"], 200)
        self.assertIn("indoorUnit", recvs[0]["body"]["r"])
        self.assertIsInstance(recvs[0]["elapsed_ms"], int)

    def test_probe_captured(self):
        creds = {
            "password": b"test",
            "crypto_serial": bytearray.fromhex(_CFG["crypto_serial"]),
        }
        self.assertTrue(probe_ip(self.adapter.address, creds, timeout=1.0))
        self.assertEqual(
            [(e["direction"], e["probe"]) for e in self.capture.events],
            [("send", True), ("recv", True)],
        )


class TestCloudTraffic(_TrafficTestCase):
    @patch("pykumo.py_kumo_cloud_account_v3.requests.post")
    def test_login_captured_with_credentials_redacted(self, mock_post):
        resp = MagicMock()
        resp.ok = True
        resp.status_code = 200
        resp.text = json.dumps({"token": {"access": "AAA", "refresh": "RRR"}})
        resp.json.return_value = json.loads(resp.text)
        mock_post.return_value = resp

        self.assertTrue(KumoCloudV3("me@example.com", "secret").login())

        send, recv = self.capture.events
        self.assertEqual((send["method"], send["path"]), ("POST", "/v3/login"))
        self.assertEqual(send["body"]["username"], traffic.REDACTED)
        self.assertEqual(send["body"]["password"], traffic.REDACTED)
        self.assertEqual(send["body"]["appVersion"], "3.2.4")
        self.assertEqual(recv["status"], 200)
        self.assertEqual(recv["body"]["token"], traffic.REDACTED)
        for message in self.capture.messages:
            self.assertNotIn("secret", message)
            self.assertNotIn("AAA", message)

    @patch("pykumo.py_kumo_cloud_account_v3.requests.Session")
    def test_socketio_frames_captured_and_decoded(self, mock_session_cls):
        session = mock_session_cls.return_value

        def _resp(text):
            resp = MagicMock()
            resp.ok = True
            resp.status_code = 200
            resp.text = text
            return resp

        session.get.side_effect = [
            _resp('0{"sid":"SID1","pingInterval":25000}'),
            _resp('40{"sid":"NS1"}'),
            _resp("6"),
            _resp('42["adapter_update",{"deviceSerial":"S1","password":"pw"}]'),
        ]
        v3 = KumoCloudV3("u", "p")
        v3._access_token = "not-a-jwt"

        self.assertEqual(
            v3.get_passwords_via_websocket(["S1"], timeout_secs=5), {"S1": "pw"}
        )

        recv = [e for e in self.capture.events if e["direction"] == "recv"]
        sent = [e for e in self.capture.events if e["direction"] == "send"]
        self.assertTrue(all(e["channel"] == "socketio" for e in self.capture.events))
        self.assertEqual(recv[0]["stage"], "handshake")
        self.assertEqual(recv[0]["packets"][0]["data"]["sid"], "SID1")
        update = recv[-1]["packets"][0]
        self.assertEqual(update["event"], "adapter_update")
        self.assertEqual(update["args"][0]["deviceSerial"], "S1")
        self.assertEqual(update["args"][0]["password"], traffic.REDACTED)
        sent_events = [p.get("event") for e in sent for p in e["packets"]]
        self.assertIn("subscribe", sent_events)
        self.assertIn("force_adapter_request", sent_events)
        self.assertIn("device_status_v2", sent_events)
        for message in self.capture.messages:
            self.assertNotIn('"pw"', message)
