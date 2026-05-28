"""Unit tests for the supplier API response parsers.

These tests exercise the pure parsing helpers in ``api.py`` and do not
require Home Assistant or network access.
"""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG_DIR = REPO_ROOT / "custom_components" / "vattenfall"


def _load_api_module():
    """Load ``api.py`` in isolation, stubbing out its HA dependency."""
    if "homeassistant" not in sys.modules:
        sys.modules["homeassistant"] = types.ModuleType("homeassistant")
    if "homeassistant.const" not in sys.modules:
        ha_const = types.ModuleType("homeassistant.const")
        ha_const.CONF_PASSWORD = "password"
        sys.modules["homeassistant.const"] = ha_const

    for pkg_name, pkg_path in (
        ("custom_components", REPO_ROOT / "custom_components"),
        ("custom_components.vattenfall", PKG_DIR),
    ):
        if pkg_name not in sys.modules:
            mod = types.ModuleType(pkg_name)
            mod.__path__ = [str(pkg_path)]
            sys.modules[pkg_name] = mod

    def load(modname: str, filename: str):
        spec = importlib.util.spec_from_file_location(modname, PKG_DIR / filename)
        assert spec and spec.loader
        m = importlib.util.module_from_spec(spec)
        sys.modules[modname] = m
        spec.loader.exec_module(m)
        return m

    load("custom_components.vattenfall.const", "const.py")
    return load("custom_components.vattenfall.api", "api.py")


api = _load_api_module()


class ParsePremisesTests(unittest.TestCase):
    def test_top_level_list(self):
        result = api._parse_premises(
            [
                {"premiseId": "STH123", "address": "Foo 1"},
                {"PremiseId": "STH456"},
            ]
        )
        self.assertEqual([p.premise_id for p in result], ["STH123", "STH456"])
        self.assertEqual(result[0].address, "Foo 1")
        self.assertIsNone(result[1].address)

    def test_nested_premises_key(self):
        result = api._parse_premises({"premises": [{"premiseId": "STH789"}]})
        self.assertEqual([p.premise_id for p in result], ["STH789"])

    def test_nested_address_dict_is_flattened(self):
        result = api._parse_premises(
            [
                {
                    "premiseId": "STH1",
                    "address": {
                        "street": "Storgatan 1",
                        "city": "Stockholm",
                        "postalCode": "11122",
                    },
                }
            ]
        )
        self.assertEqual(result[0].address, "Storgatan 1, Stockholm, 11122")

    def test_skips_entries_without_id(self):
        result = api._parse_premises([{"address": "no id"}])
        self.assertEqual(result, [])


class ParseHourlyTests(unittest.TestCase):
    def test_sorted_by_datetime(self):
        result = api._parse_hourly(
            {
                "items": [
                    {
                        "year": 2026,
                        "month": 5,
                        "day": 24,
                        "hour": 1,
                        "measurement": {"value": 0.119, "unit": "KWH"},
                    },
                    {
                        "year": 2026,
                        "month": 5,
                        "day": 24,
                        "hour": 0,
                        "measurement": {"value": 0.5, "unit": "KWH"},
                    },
                ]
            }
        )
        self.assertEqual(
            [p.date_time for p in result],
            ["2026-05-24T00:00:00", "2026-05-24T01:00:00"],
        )
        self.assertAlmostEqual(result[0].value_kwh, 0.5)

    def test_skips_malformed(self):
        result = api._parse_hourly({"items": [{"foo": "bar"}]})
        self.assertEqual(result, [])

    def test_empty_payload(self):
        self.assertEqual(api._parse_hourly({}), [])
        self.assertEqual(api._parse_hourly([]), [])


class ParseDailyTests(unittest.TestCase):
    def test_basic(self):
        result = api._parse_daily(
            {
                "items": [
                    {
                        "year": 2026,
                        "month": 5,
                        "day": 2,
                        "measurement": {"value": 3.162, "unit": "KWH"},
                    },
                    {
                        "year": 2026,
                        "month": 5,
                        "day": 1,
                        "measurement": {"value": 3.539, "unit": "KWH"},
                    },
                ]
            }
        )
        self.assertEqual([p.date for p in result], ["2026-05-01", "2026-05-02"])
        self.assertAlmostEqual(result[1].value_kwh, 3.162)


class ResolvePremiseTests(unittest.TestCase):
    def test_falls_back_to_instance(self):
        client = api.VattenfallApiClient(
            customer_id="c", password="p", premise_id="STH1"
        )
        self.assertEqual(client._resolve_premise(None), "STH1")
        self.assertEqual(client._resolve_premise("STH2"), "STH2")

    def test_raises_when_missing(self):
        client = api.VattenfallApiClient(customer_id="c", password="p")
        with self.assertRaises(api.VattenfallApiError):
            client._resolve_premise(None)


class LastFinalizedDayTests(unittest.TestCase):
    def test_picks_minimum_hourly_epoch(self):
        ranges = [
            {"resolution": "Hourly", "lastAvailableMeasurementDate": 1779580800.0},
            {"resolution": "Hourly", "lastAvailableMeasurementDate": 1779667200.0},
            {"resolution": "Daily", "lastAvailableMeasurementDate": 1779667200.0},
        ]
        # 1779580800 = 2026-05-24 UTC midnight
        self.assertEqual(
            api._last_finalized_day_from_ranges(ranges).isoformat(),
            "2026-05-24",
        )

    def test_ignores_non_hourly_resolutions(self):
        ranges = [
            {"resolution": "Daily", "lastAvailableMeasurementDate": 1.0},
            {"resolution": "Yearly", "lastAvailableMeasurementDate": 2.0},
        ]
        self.assertIsNone(api._last_finalized_day_from_ranges(ranges))

    def test_returns_none_for_empty(self):
        self.assertIsNone(api._last_finalized_day_from_ranges([]))

    def test_handles_malformed_entries(self):
        ranges = [
            "not a dict",
            {"resolution": "Hourly"},
            {"resolution": "Hourly", "lastAvailableMeasurementDate": "nope"},
            {"resolution": "Hourly", "lastAvailableMeasurementDate": 1779580800.0},
        ]
        self.assertEqual(
            api._last_finalized_day_from_ranges(ranges).isoformat(),
            "2026-05-24",
        )

    def test_resolution_match_is_case_insensitive(self):
        ranges = [
            {"resolution": "hourly", "lastAvailableMeasurementDate": 1779580800.0},
        ]
        self.assertEqual(
            api._last_finalized_day_from_ranges(ranges).isoformat(),
            "2026-05-24",
        )


if __name__ == "__main__":
    unittest.main()
