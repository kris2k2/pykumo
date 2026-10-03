"""Tests for what update_status() asks the adapter for, and setpoint limits.

Adapter responses are taken from a traffic capture of a ducted P-series unit
(PEFY) with no wireless sensor and no MHK2 thermostat.
"""

import copy
import json
import unittest
from unittest.mock import patch

from pykumo import PyKumo
from pykumo.const import MHK2_RECHECK_SECONDS, PROFILE_REFRESH_SECONDS

_CFG = {
    "password": "dGVzdA==",  # base64("test")
    "crypto_serial": "0123456789ABCDEF01234567",
}

_STATUS = {
    "roomTemp": 24.5,
    "mode": "cool",
    "spCool": 25.5,
    "spHeat": 24,
    "vaneDir": "horizontal",
    "fanSpeed": "auto",
    "tempSource": "unset",
    "activeThermistor": "unset",
    "filterDirty": True,
    "defrost": False,
    "standby": False,
}
_PROFILE = {
    "hasModeDry": True,
    "hasModeHeat": False,
    "hasVaneDir": False,
    "hasVaneSwing": False,
    "hasModeVent": True,
    "hasFanSpeedAuto": True,
    "numberOfFanSpeeds": 3,
    "maximumSetPoints": {"cool": 30, "heat": 28, "auto": 28},
    "minimumSetPoints": {"cool": 19, "heat": 17, "auto": 19},
}
_ADAPTER_STATUS = {
    "localNetwork": {"stationMode": {"RSSI": -42, "SSID": "ssid"}},
    "autoModePrevention": True,
    "userMinCoolSetPoint": 19.5,
    "userMaxHeatSetPoint": 28,
    "runState": "normal",
    "userHasModeDry": True,
    "userHasModeHeat": False,
}
_NO_SENSOR = {
    "uuid": None,
    "rssi": None,
    "txPower": None,
    "battery": None,
    "temperature": None,
    "humidity": None,
}
_NO_MHK2 = {"status": {"outdoorTemp": None, "outdoorHumid": None, "indoorHumid": None}}


class _FakeAdapter:
    """Answers update_status() queries and records which ones were asked."""

    def __init__(self, mhk2=_NO_MHK2):
        self.mhk2 = mhk2
        self.profile = copy.deepcopy(_PROFILE)
        self.adapter_status = copy.deepcopy(_ADAPTER_STATUS)
        self.queries = []
        self.failing = set()  # top-level keys to answer with {}

    def request(self, post_data):
        query = json.loads(post_data)["c"]
        if self.failing & query.keys():
            return {}
        if "indoorUnit" in query:
            part = next(iter(query["indoorUnit"]))
            self.queries.append(part)
            body = _STATUS if part == "status" else self.profile
            return {"r": {"indoorUnit": {part: dict(body)}}}
        if "sensors" in query:
            self.queries.append("sensors")
            return {"r": {"sensors": {"0": dict(_NO_SENSOR)}}}
        if "adapter" in query:
            self.queries.append("adapter")
            return {"r": {"adapter": {"status": dict(self.adapter_status)}}}
        if "mhk2" in query:
            self.queries.append("mhk2")
            return {"r": {"mhk2": self.mhk2}}
        raise AssertionError(f"unexpected query {query}")


class TestPollCycle(unittest.TestCase):
    def setUp(self):
        self.adapter = _FakeAdapter()
        self.unit = PyKumo("Apartment", "192.0.2.1", _CFG)
        patcher = patch.object(PyKumo, "_request", side_effect=self.adapter.request)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.now = 1000.0
        clock = patch("pykumo.py_kumo.time.monotonic", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def poll(self, advance=60):
        """Run one poll, `advance` seconds after the previous one."""
        self.now += advance
        self.adapter.queries.clear()
        self.assertTrue(self.unit.update_status())
        return list(self.adapter.queries)

    def test_first_poll_reads_everything(self):
        self.assertEqual(
            self.poll(), ["status", "sensors", "profile", "adapter", "mhk2"]
        )

    def test_later_polls_skip_profile_and_absent_mhk2(self):
        self.poll()
        self.assertEqual(self.poll(), ["status", "sensors", "adapter"])

    def test_profile_reread_after_refresh_interval(self):
        self.poll()
        self.assertIn("profile", self.poll(advance=PROFILE_REFRESH_SECONDS + 1))

    def test_partial_profile_reread_next_poll(self):
        del self.adapter.profile["maximumSetPoints"]
        self.poll()
        self.assertIn("profile", self.poll())

    def test_absent_mhk2_rechecked_after_interval(self):
        self.poll()
        self.assertIn("mhk2", self.poll(advance=MHK2_RECHECK_SECONDS + 1))

    def test_present_mhk2_polled_every_time(self):
        self.adapter.mhk2 = {
            "status": {"outdoorTemp": None, "outdoorHumid": None, "indoorHumid": 41}
        }
        self.poll()
        self.assertIn("mhk2", self.poll())
        self.assertEqual(self.unit.get_current_humidity(), 41)

    def test_cached_profile_keeps_adapter_overrides(self):
        self.poll()
        self.poll()
        self.assertFalse(self.unit.has_heat_mode())
        self.assertFalse(self.unit.has_auto_mode())
        self.assertTrue(self.unit.has_dry_mode())
        self.assertEqual(self.unit.get_wifi_rssi(), -42)
        self.assertEqual(self.unit.get_runstate(), "normal")
        # The adapter-status overrides must not leak into the cached profile.
        self.assertNotIn("wifiRSSI", self.unit._raw_profile)
        self.assertNotIn("hasModeAuto", self.unit._raw_profile)

    def test_failed_adapter_status_leaves_profile_alone(self):
        self.poll()
        before = dict(self.unit._profile)
        self.adapter.failing = {"adapter"}
        self.now += PROFILE_REFRESH_SECONDS + 1
        self.assertFalse(self.unit.update_status())
        self.assertEqual(self.unit._profile, before)


class TestModes(unittest.TestCase):
    """Which modes update_status() finds the unit offers."""

    def setUp(self):
        self.adapter = _FakeAdapter()
        self.unit = PyKumo("Apartment", "192.0.2.1", _CFG)
        patcher = patch.object(PyKumo, "_request", side_effect=self.adapter.request)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.now = 1000.0
        clock = patch("pykumo.py_kumo.time.monotonic", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def poll(self, advance=60):
        self.now += advance
        self.assertTrue(self.unit.update_status())
        return self.modes()

    def modes(self):
        checks = {
            "dry": self.unit.has_dry_mode,
            "heat": self.unit.has_heat_mode,
            "vent": self.unit.has_vent_mode,
            "auto": self.unit.has_auto_mode,
        }
        return {mode for mode, check in checks.items() if check()}

    def heat_pump(self):
        self.adapter.profile["hasModeHeat"] = True
        self.adapter.adapter_status["userHasModeHeat"] = True

    def test_cooling_only_unit(self):
        # The PEFY's profile has auto setpoints, but with no heat mode there
        # is nothing for auto to switch to.
        self.assertEqual(self.poll(), {"dry", "vent"})
        with patch.object(PyKumo, "_request") as request:
            self.assertEqual(self.unit.set_mode("auto"), {})
            self.assertEqual(self.unit.set_mode("heat"), {})
        request.assert_not_called()

    def test_cooling_only_unit_without_auto_prevention(self):
        self.adapter.adapter_status["autoModePrevention"] = False
        self.assertNotIn("auto", self.poll())

    def test_heat_pump_with_auto_setpoints(self):
        self.heat_pump()
        self.assertEqual(self.poll(), {"dry", "heat", "vent", "auto"})

    def test_heat_pump_with_auto_prevented(self):
        self.heat_pump()
        for profile_key in ("maximumSetPoints", "minimumSetPoints"):
            del self.adapter.profile[profile_key]["auto"]
        self.assertNotIn("auto", self.poll())

    def test_heat_turned_off_in_app_also_turns_off_auto(self):
        self.heat_pump()
        self.adapter.adapter_status["userHasModeHeat"] = False
        self.assertEqual(self.poll(), {"dry", "vent"})

    def test_dry_turned_off_in_app(self):
        self.adapter.adapter_status["userHasModeDry"] = False
        self.assertEqual(self.poll(), {"vent"})

    def test_missing_user_flag_keeps_dry(self):
        del self.adapter.adapter_status["userHasModeDry"]
        self.assertIn("dry", self.poll())

    def test_missing_flags_keep_last_value_seen(self):
        self.heat_pump()
        self.adapter.adapter_status["userHasModeDry"] = False
        self.adapter.adapter_status["autoModePrevention"] = False
        self.assertEqual(self.poll(), {"heat", "vent", "auto"})
        for key in ("userHasModeDry", "userHasModeHeat", "autoModePrevention"):
            del self.adapter.adapter_status[key]
        self.assertEqual(self.poll(), {"heat", "vent", "auto"})

    def test_partial_profile_reread_keeps_dry(self):
        self.assertIn("dry", self.poll())
        del self.adapter.profile["hasModeDry"]
        self.assertIn("dry", self.poll(advance=PROFILE_REFRESH_SECONDS + 1))


def _unit_with_profile(**overrides):
    unit = PyKumo("Unit", "192.0.2.1", _CFG)
    profile = {
        **_PROFILE,
        "hasModeHeat": True,
        "hasModeAuto": True,
        "userMinCoolSetPoint": 19.5,
        "userMaxHeatSetPoint": 26,
    }
    profile.update(overrides)
    unit._profile = profile
    return unit


class TestSetpointLimits(unittest.TestCase):
    def test_unknown_before_profile(self):
        self.assertIsNone(PyKumo("Unit", "192.0.2.1", _CFG).get_setpoint_limits())

    def test_cool_honors_user_minimum(self):
        unit = _unit_with_profile()
        self.assertEqual(unit.get_setpoint_limits("cool"), (19.5, 30))
        self.assertEqual(unit.get_setpoint_limits("dry"), (19.5, 30))

    def test_heat_honors_user_maximum(self):
        unit = _unit_with_profile()
        self.assertEqual(unit.get_setpoint_limits("heat"), (17, 26))

    def test_auto_uses_auto_range(self):
        unit = _unit_with_profile()
        for mode in ("auto", "autoCool", "autoHeat"):
            self.assertEqual(unit.get_setpoint_limits(mode), (19, 28))

    def test_off_spans_supported_modes(self):
        unit = _unit_with_profile()
        self.assertEqual(unit.get_setpoint_limits("off"), (17, 30))
        unit = _unit_with_profile(hasModeHeat=False, hasModeAuto=False)
        self.assertEqual(unit.get_setpoint_limits("vent"), (19.5, 30))

    def test_user_limits_only_narrow(self):
        unit = _unit_with_profile(userMinCoolSetPoint=16, userMaxHeatSetPoint=31)
        self.assertEqual(unit.get_setpoint_limits("cool"), (19, 30))
        self.assertEqual(unit.get_setpoint_limits("heat"), (17, 28))

    def test_defaults_to_current_mode(self):
        unit = _unit_with_profile()
        unit._status = {"mode": "heat"}
        self.assertEqual(unit.get_setpoint_limits(), (17, 26))

    def test_missing_mode_in_profile(self):
        unit = _unit_with_profile(
            maximumSetPoints={"cool": 30}, minimumSetPoints={"cool": 19}
        )
        self.assertIsNone(unit.get_setpoint_limits("heat"))
        self.assertEqual(unit.get_setpoint_limits("off"), (19.5, 30))


if __name__ == "__main__":
    unittest.main()
