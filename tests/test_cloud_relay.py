"""Tests for sending local-API requests through Kumo Cloud's relay-command."""

import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

from pykumo import relay_probe
from pykumo.py_kumo import PyKumo
from pykumo.py_kumo_base import PyKumoBase
from pykumo.py_kumo_cloud_account import KumoCloudAccount
from pykumo.py_kumo_cloud_account_v3 import V3_BASE_URL, KumoCloudV3

_CFG = {
    "password": "dGVzdA==",  # base64("test")
    "crypto_serial": "0123456789ABCDEF01234567",
}


def _resp(status, body):
    resp = MagicMock()
    resp.status_code = status
    resp.ok = 200 <= status < 300
    resp.json.return_value = body
    resp.text = str(body)
    return resp


@patch("pykumo.py_kumo_cloud_account_v3.requests.request")
class TestRelayCommand(unittest.TestCase):
    def _v3(self):
        v3 = KumoCloudV3("u", "p")
        v3._access_token = "access"
        v3._refresh_token = "refresh"
        return v3

    def test_posts_command_with_serial(self, mock_request):
        mock_request.return_value = _resp(200, {"adapter": {"status": {}}})
        result = self._v3().relay_command("S1", {"adapter": {"status": {}}})

        self.assertEqual(result, {"adapter": {"status": {}}})
        method, url = mock_request.call_args.args
        self.assertEqual(method, "POST")
        self.assertEqual(url, f"{V3_BASE_URL}/v3/devices/S1/relay-command")
        kwargs = mock_request.call_args.kwargs
        self.assertEqual(kwargs["json"], {"serial": "S1", "adapter": {"status": {}}})
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer access")

    def test_refreshes_and_retries_on_401(self, mock_request):
        mock_request.side_effect = [
            _resp(401, {"error": "notAuthorized"}),
            _resp(200, {"ok": True}),
        ]
        v3 = self._v3()

        def refresh():
            v3._access_token = "new-access"
            return True

        with patch.object(v3, "refresh", side_effect=refresh):
            result = v3.relay_command("S1", {"adapter": {"status": {}}})

        self.assertEqual(result, {"ok": True})
        self.assertEqual(mock_request.call_count, 2)
        retry_headers = mock_request.call_args.kwargs["headers"]
        self.assertEqual(retry_headers["Authorization"], "Bearer new-access")

    def test_http_error_returns_none(self, mock_request):
        mock_request.return_value = _resp(404, {"error": "deviceNotFound"})
        with self.assertLogs("pykumo.py_kumo_cloud_account_v3", "WARNING"):
            result = self._v3().relay_command("S1", {"adapter": {"status": {}}})
        self.assertIsNone(result)

    def test_logs_in_first_without_a_token(self, mock_request):
        mock_request.return_value = _resp(200, {})
        v3 = KumoCloudV3("u", "p")
        with patch.object(v3, "login", return_value=False) as login:
            self.assertIsNone(v3.relay_command("S1", {}))
        login.assert_called_once()
        mock_request.assert_not_called()

    def test_get_still_goes_through_call(self, mock_request):
        mock_request.return_value = _resp(200, [{"id": "site"}])
        self.assertEqual(self._v3().get_sites(), [{"id": "site"}])
        self.assertEqual(mock_request.call_args.args[0], "GET")
        self.assertIsNone(mock_request.call_args.kwargs["json"])


class TestUnitThroughRelay(unittest.TestCase):
    def test_sends_contents_of_c_and_wraps_answer(self):
        relay = MagicMock()
        relay.relay_command.return_value = {"indoorUnit": {"status": {"mode": "off"}}}
        unit = PyKumoBase("Unit", None, None, serial="S1", cloud_relay=relay)

        with patch("pykumo.py_kumo_base.requests.Session") as session:
            result = unit._request(b'{"c":{"indoorUnit":{"status":{}}}}')

        relay.relay_command.assert_called_once_with(
            "S1", {"indoorUnit": {"status": {}}}
        )
        self.assertEqual(result, {"r": {"indoorUnit": {"status": {"mode": "off"}}}})
        session.assert_not_called()
        self.assertIsNotNone(unit.get_request_latency())

    def test_answer_already_in_local_shape_is_kept(self):
        relay = MagicMock()
        relay.relay_command.return_value = {"r": {"adapter": {"status": {}}}}
        unit = PyKumoBase("Unit", None, _CFG, serial="S1", cloud_relay=relay)
        self.assertEqual(
            unit._request(b'{"c":{"adapter":{"status":{}}}}'),
            {"r": {"adapter": {"status": {}}}},
        )

    def test_failed_relay_returns_empty(self):
        relay = MagicMock()
        relay.relay_command.return_value = None
        unit = PyKumoBase("Unit", None, None, serial="S1", cloud_relay=relay)
        self.assertEqual(unit._request(b'{"c":{"adapter":{"status":{}}}}'), {})

    def test_request_without_c_is_not_relayed(self):
        relay = MagicMock()
        unit = PyKumoBase("Unit", None, None, serial="S1", cloud_relay=relay)
        with self.assertLogs("pykumo.py_kumo_base", "WARNING"):
            self.assertEqual(unit._request(b"not json"), {})
        relay.relay_command.assert_not_called()

    def test_relay_needs_serial(self):
        with self.assertRaises(ValueError):
            PyKumoBase("Unit", None, None, cloud_relay=MagicMock())

    def test_pykumo_passes_relay_through(self):
        relay = MagicMock()
        relay.relay_command.return_value = {}
        unit = PyKumo("Unit", None, None, serial="S1", cloud_relay=relay)
        unit._request(b'{"c":{"adapter":{"status":{}}}}')
        relay.relay_command.assert_called_once()

    def test_make_pykumos_with_cloud_relay(self):
        cached = [
            {},
            {},
            {
                "children": [
                    {
                        "zoneTable": {
                            "S1": {
                                "serial": "S1",
                                "label": "Unit 1",
                                "address": "192.168.1.10",
                                "password": "dGVzdA==",
                                "cryptoSerial": "0123456789ABCDEF01",
                                "unitType": "ductless",
                            }
                        }
                    }
                ]
            },
        ]
        account = KumoCloudAccount("u", "p", kumo_dict=cached)
        with patch("pykumo.py_kumo_cloud_account.KumoCloudV3") as v3_cls:
            kumos = account.make_pykumos(init_update_status=False, cloud_relay=True)
        v3_cls.assert_called_once_with("u", "p")
        self.assertIs(kumos["Unit 1"]._cloud_relay, v3_cls.return_value)

        local = account.make_pykumos(init_update_status=False)
        self.assertIsNone(local["Unit 1"]._cloud_relay)


class TestRelayProbeHelpers(unittest.TestCase):
    def test_is_read_only(self):
        self.assertTrue(relay_probe.is_read_only({"adapter": {"status": {}}}))
        self.assertTrue(relay_probe.is_read_only({"adapter": {}}))
        self.assertFalse(
            relay_probe.is_read_only({"adapter": {"status": {"ledDisabled": True}}})
        )
        self.assertFalse(relay_probe.is_read_only({"adapter": {"status": []}}))

    def test_parse_query_strips_c(self):
        self.assertEqual(
            relay_probe.parse_query('{"c":{"adapter":{"status":{}}}}'),
            {"adapter": {"status": {}}},
        )
        self.assertEqual(
            relay_probe.parse_query('{"adapter":{"info":{}}}'),
            {"adapter": {"info": {}}},
        )
        with self.assertRaises(ValueError):
            relay_probe.parse_query("[1]")

    def test_find_and_mask_secrets(self):
        answer = {
            "adapter": {
                "status": {"password": "cHcx", "name": "Loft"},
                "info": {"cryptoSerial": "0123456789ABCDEF01", "cryptoKeySet": "F"},
            }
        }
        found = dict(relay_probe.find_secrets(answer))
        self.assertEqual(
            found,
            {
                ("adapter", "status", "password"): "cHcx",
                ("adapter", "info", "cryptoSerial"): "0123456789ABCDEF01",
                ("adapter", "info", "cryptoKeySet"): "F",
            },
        )
        masked = relay_probe.mask_secrets(answer)
        self.assertEqual(masked["adapter"]["status"]["password"], "**MASKED**")
        self.assertEqual(masked["adapter"]["status"]["name"], "Loft")
        self.assertEqual(masked["adapter"]["info"]["cryptoSerial"], "**MASKED**")

    def test_credentials_from(self):
        creds = relay_probe.credentials_from(
            {
                ("a", "password"): "dGVzdA==",
                ("b", "cryptoSerial"): "0123456789ABCDEF01",
            }
        )
        self.assertEqual(creds["password"], b"test")
        self.assertEqual(
            creds["crypto_serial"], bytearray.fromhex("0123456789ABCDEF01")
        )
        # A password that isn't base64 is used as is.
        raw = relay_probe.credentials_from(
            {("password",): "plain pw!", ("cryptoSerial",): "00112233445566778899"}
        )
        self.assertEqual(raw["password"], b"plain pw!")
        self.assertIsNone(relay_probe.credentials_from({("password",): "x"}))

    def test_probe_unit_verifies_found_credentials(self):
        v3 = MagicMock()
        v3.relay_command.return_value = {
            "adapter": {
                "status": {
                    "password": "dGVzdA==",
                    "cryptoSerial": "0123456789ABCDEF01",
                }
            }
        }
        out = io.StringIO()
        with patch.object(relay_probe, "probe_ip", return_value=True) as probe:
            with redirect_stdout(out):
                found = relay_probe.probe_unit(
                    v3,
                    "S1",
                    "Unit",
                    [{"adapter": {"status": {}}}],
                    show_secrets=False,
                    verify_address="10.0.0.5",
                )
        self.assertEqual(len(found), 2)
        probe.assert_called_once()
        self.assertEqual(probe.call_args.args[0], "10.0.0.5")
        text = out.getvalue()
        self.assertIn("ACCEPTED", text)
        self.assertNotIn("dGVzdA==", text)

    def test_probe_unit_flags_an_echo(self):
        v3 = MagicMock()
        v3.relay_command.return_value = {"adapter": {"status": {}}}
        out = io.StringIO()
        with redirect_stdout(out):
            found = relay_probe.probe_unit(
                v3, "S1", "Unit", [{"adapter": {"status": {}}}], False
            )
        self.assertEqual(found, {})
        self.assertIn("an echo of the query", out.getvalue())

    def test_main_refuses_writes_without_flag(self):
        with redirect_stdout(io.StringIO()), patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                relay_probe.main(
                    [
                        "--username",
                        "u",
                        "--query",
                        '{"adapter":{"status":{"ledDisabled":true}}}',
                    ]
                )
        self.assertEqual(cm.exception.code, 2)

    @patch.dict("os.environ", {"KUMO_PASSWORD": "pw"})
    @patch.object(relay_probe, "KumoCloudV3")
    def test_main_queries_every_unit(self, v3_cls):
        v3 = v3_cls.return_value
        v3.login.return_value = True
        v3.get_sites.return_value = [{"id": "site"}]
        v3.get_zones.return_value = [
            {"name": "Loft", "adapter": {"deviceSerial": "S1"}},
            {"name": "Den", "adapter": {"deviceSerial": "S2"}},
        ]
        v3.relay_command.return_value = {}
        with redirect_stdout(io.StringIO()):
            code = relay_probe.main(["--username", "u"])
        self.assertEqual(code, 0)
        v3_cls.assert_called_once_with("u", "pw")
        serials = {c.args[0] for c in v3.relay_command.call_args_list}
        self.assertEqual(serials, {"S1", "S2"})
        self.assertEqual(
            v3.relay_command.call_count, 2 * len(relay_probe.DEFAULT_QUERIES)
        )


if __name__ == "__main__":
    unittest.main()
