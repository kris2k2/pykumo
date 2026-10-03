"""Tests for how long the V3 Socket.IO session waits for adapter passwords."""

import unittest
from unittest.mock import MagicMock, patch

from pykumo.py_kumo_cloud_account_v3 import KumoCloudV3

# adapter_update as the cloud sent it in a 2026 capture: no password.
_UPDATE_NO_PASSWORD = (
    '42["adapter_update",{"deviceSerial":"S1","firmwareVersion":"02.06.26",'
    '"routerRssi":-46,"minSetpoint":19.5,"maxSetpoint":28}]'
)
_UPDATE_WITH_PASSWORD = '42["adapter_update",{"deviceSerial":"S1","password":"pw"}]'


def _resp(text):
    resp = MagicMock()
    resp.ok = True
    resp.status_code = 200
    resp.text = text
    return resp


@patch("pykumo.py_kumo_cloud_account_v3.requests.Session")
class TestPasswordWait(unittest.TestCase):
    def _run(self, mock_session_cls, replies, serials=("S1",), timeout_secs=60):
        session = mock_session_cls.return_value
        session.get.side_effect = [_resp(r) for r in replies]
        v3 = KumoCloudV3("u", "p")
        v3._access_token = "not-a-jwt"
        result = v3.get_passwords_via_websocket(list(serials), timeout_secs)
        return result, session

    def test_stops_once_device_answers_without_password(self, mock_session_cls):
        result, session = self._run(
            mock_session_cls,
            [
                '0{"sid":"SID1","pingInterval":25000}',
                '40{"sid":"NS1"}',
                "6",  # reply to the device subscribe
                _UPDATE_NO_PASSWORD,
            ],
        )
        self.assertEqual(result, {})
        # No further long poll after the device answered.
        self.assertEqual(session.get.call_count, 4)

    def test_early_update_without_password_does_not_end_wait(self, mock_session_cls):
        # An adapter_update that arrives before force_adapter_request was sent
        # isn't the answer to it, so keep waiting.
        result, session = self._run(
            mock_session_cls,
            [
                '0{"sid":"SID1","pingInterval":25000}',
                '40{"sid":"NS1"}',
                _UPDATE_NO_PASSWORD,
                "2",  # ping
                _UPDATE_WITH_PASSWORD,
            ],
        )
        self.assertEqual(result, {"S1": "pw"})
        self.assertEqual(session.get.call_count, 5)

    def test_waits_for_every_device(self, mock_session_cls):
        result, session = self._run(
            mock_session_cls,
            [
                '0{"sid":"SID1","pingInterval":25000}',
                '40{"sid":"NS1"}',
                "6",
                _UPDATE_NO_PASSWORD,
                '42["adapter_update",{"deviceSerial":"S2","password":"pw2"}]',
            ],
            serials=("S1", "S2"),
        )
        self.assertEqual(result, {"S2": "pw2"})
        self.assertEqual(session.get.call_count, 5)

    def test_long_poll_timeout_follows_ping_interval(self, mock_session_cls):
        _, session = self._run(
            mock_session_cls,
            [
                '0{"sid":"SID1","pingInterval":25000}',
                '40{"sid":"NS1"}',
                "6",
                _UPDATE_WITH_PASSWORD,
            ],
        )
        # A poll can legitimately sit for the whole ping interval, so it must
        # not time out before then.
        self.assertEqual(session.get.call_args_list[-1].kwargs["timeout"], 30)


if __name__ == "__main__":
    unittest.main()
