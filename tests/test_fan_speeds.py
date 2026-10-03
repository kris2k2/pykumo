"""Tests for get_fan_speeds() across all numberOfFanSpeeds values."""

import json
import unittest
from unittest.mock import MagicMock, patch
from pykumo.py_kumo import PyKumo


def make_unit(num_speeds, has_auto=False):
    """Return a minimally-mocked PyKumo with the given profile."""
    with patch.object(PyKumo, "__init__", lambda self, *a, **kw: None):
        unit = PyKumo.__new__(PyKumo)
    profile = {"numberOfFanSpeeds": num_speeds}
    if has_auto:
        profile["hasFanSpeedAuto"] = True
    unit._profile = profile
    unit._status = {}
    return unit


class TestGetFanSpeeds(unittest.TestCase):
    def test_3speed_declared(self):
        """Units reporting numberOfFanSpeeds=3 get the three they declare."""
        unit = make_unit(3)
        self.assertEqual(unit.get_fan_speeds(), ["quiet", "low", "powerful"])

    def test_3speed_with_auto(self):
        """3-speed + hasFanSpeedAuto appends auto at end."""
        unit = make_unit(3, has_auto=True)
        self.assertEqual(unit.get_fan_speeds(), ["quiet", "low", "powerful", "auto"])

    def test_3speed_include_undeclared(self):
        """Opting in adds superQuiet and superPowerful around the declared three."""
        unit = make_unit(3, has_auto=True)
        self.assertEqual(
            unit.get_fan_speeds(include_undeclared=True),
            ["superQuiet", "quiet", "low", "powerful", "superPowerful", "auto"],
        )

    def test_4speed_unchanged(self):
        """4-speed behaviour is unchanged."""
        unit = make_unit(4)
        speeds = unit.get_fan_speeds()
        self.assertEqual(speeds, ["quiet", "Low", "powerful", "superPowerful"])

    def test_4speed_include_undeclared(self):
        """Opting in adds superQuiet to a 4-speed unit, keeping its "Low"."""
        unit = make_unit(4)
        self.assertEqual(
            unit.get_fan_speeds(include_undeclared=True),
            ["superQuiet", "quiet", "Low", "powerful", "superPowerful"],
        )

    def test_5speed_unchanged(self):
        """5-speed (default) behaviour is unchanged, with or without opting in."""
        unit = make_unit(5)
        full = ["superQuiet", "quiet", "low", "powerful", "superPowerful"]
        self.assertEqual(unit.get_fan_speeds(), full)
        self.assertEqual(unit.get_fan_speeds(include_undeclared=True), full)

    def test_include_undeclared_does_not_mutate_declared(self):
        """Asking for the wider list doesn't change later default answers."""
        unit = make_unit(3)
        unit.get_fan_speeds(include_undeclared=True)
        self.assertEqual(unit.get_fan_speeds(), ["quiet", "low", "powerful"])


class TestSetUndeclaredFanSpeed(unittest.TestCase):
    def setUp(self):
        self.unit = make_unit(3)
        self.unit._request = MagicMock(return_value={})
        self.unit._last_status_update = 0

    def _sent_speed(self):
        body = self.unit._request.call_args[0][0]
        return json.loads(body)["c"]["indoorUnit"]["status"]["fanSpeed"]

    def test_undeclared_speed_is_still_sent(self):
        """superQuiet on a 3-speed unit is sent, and logged below warning."""
        with self.assertLogs("pykumo.py_kumo", level="INFO") as logs:
            self.unit.set_fan_speed("superQuiet")
        self.assertEqual(self._sent_speed(), "superQuiet")
        self.assertEqual([r.levelname for r in logs.records], ["INFO"])

    def test_speed_the_unit_lacks_warns(self):
        """A known speed outside even the undeclared range still warns."""
        with self.assertLogs("pykumo.py_kumo", level="INFO") as logs:
            self.unit.set_fan_speed("auto")
        self.assertEqual(self._sent_speed(), "auto")
        self.assertEqual([r.levelname for r in logs.records], ["WARNING"])


if __name__ == "__main__":
    unittest.main()
