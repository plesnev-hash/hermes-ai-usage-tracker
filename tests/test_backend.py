from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

import agent.account_usage as core_account_usage

REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("ai_usage_tracker_api_under_test", REPO / "dashboard" / "plugin_api.py")
assert SPEC and SPEC.loader
api = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(api)


class QuotaParserTests(unittest.TestCase):
    def stub_payload(self, payload: dict) -> None:
        # Test doubles only: no credential or network access.
        api._runtime_key = lambda _provider: "test-key"
        api._get_json = lambda *_args, **_kwargs: payload

    def test_zai_weekly_tokens_are_not_labeled_as_session(self) -> None:
        self.stub_payload({
            "data": {
                "level": "pro",
                "limits": [
                    {"type": "CREDIT_LIMIT", "unit": 3, "number": 5, "percentage": 1,
                     "usage": 12000, "currentValue": 197, "remaining": 11802,
                     "nextResetTime": 1794092496081},
                    {"type": "TOKENS_LIMIT", "unit": 6, "number": 1, "percentage": 82,
                     "usage": 60000, "currentValue": 49687, "remaining": 10312,
                     "nextResetTime": 1794435994977},
                    {"type": "TIME_LIMIT", "unit": 3, "number": 5, "percentage": 4,
                     "usage": 100, "currentValue": 4, "remaining": 96},
                ],
            }
        })
        result = api._probe_zai()
        self.assertTrue(result["available"])
        self.assertEqual(result["plan"], "pro")
        self.assertEqual([row["label"] for row in result["windows"]],
                         ["Session (5h)", "Weekly", "Tool quota"])
        self.assertEqual(result["windows"][0]["remaining_percent"], 99.0)
        self.assertEqual(result["windows"][1]["remaining_percent"], 18.0)
        self.assertIn("T", result["windows"][0]["reset_at"])

    def test_anthropic_utilization_is_already_a_percentage(self) -> None:
        payload = {
            "five_hour": {"utilization": 1.0, "resets_at": "2026-11-09T01:59:59Z"},
            "seven_day": {"utilization": 0.5, "resets_at": "2026-11-15T00:59:59Z"},
        }
        self.stub_payload(payload)
        api._runtime_key = lambda _provider: "sk-ant-oat01-test-only"
        # The pre-fix plugin delegated to Hermes' parser; stub that boundary too
        # so this test reproduces the original 1% -> 100% failure exactly.
        core_account_usage._get_json = lambda *_args, **_kwargs: payload
        result = api._PROBES["anthropic"]()
        self.assertTrue(result["available"])
        self.assertEqual([row["label"] for row in result["windows"]], ["Current session", "Current week"])
        self.assertEqual([row["used_percent"] for row in result["windows"]], [1.0, 0.5])
        self.assertEqual([row["remaining_percent"] for row in result["windows"]], [99.0, 99.5])

    def test_kimi_modern_usages_and_monthly_windows(self) -> None:
        self.stub_payload({
            "usages": {
                "limit_5h": {"used_ratio": 0.4533, "reset_time": "2026-11-08T12:22:18Z"},
                "limit_month_total": {"used": 717, "limit": 1000, "resetTime": "2026-11-13T00:00:00Z"},
                "limit_month_code": {"used_ratio": 0.4398, "reset_time": "2026-11-13T00:00:00Z"},
            }
        })
        result = api._probe_kimi()
        self.assertTrue(result["available"])
        self.assertEqual([row["label"] for row in result["windows"]],
                         ["Session (5h)", "Monthly total", "Monthly coding"])
        self.assertAlmostEqual(result["windows"][0]["remaining_percent"], 54.67)
        self.assertAlmostEqual(result["windows"][1]["remaining_percent"], 28.3)
        self.assertAlmostEqual(result["windows"][2]["remaining_percent"], 56.02)

    def test_kimi_modern_usages_suppress_duplicate_legacy_limits(self) -> None:
        self.stub_payload({
            "usages": {"limit_5h": {"used_ratio": 0.25}},
            "limits": [{"percentage": 99, "window": "duplicate"}],
        })
        result = api._probe_kimi()
        self.assertEqual(len(result["windows"]), 1)
        self.assertEqual(result["windows"][0]["label"], "Session (5h)")

    def test_cursor_keeps_model_pools_separate_and_preserves_zero(self) -> None:
        reset = "2026-12-06T09:58:58.000Z"
        windows = api._cursor_quota_windows({
            "billingCycleEnd": reset,
            "individualUsage": {"plan": {
                "autoPercentUsed": 0.93,
                "apiPercentUsed": 0,
                "totalPercentUsed": 82,
                "breakdown": {"bonus": 40},
            }},
        })
        self.assertEqual([row["label"] for row in windows], ["Cursor models", "Other models"])
        self.assertEqual([row["used_percent"] for row in windows], [0.93, 0.0])
        self.assertEqual([row["reset_at"] for row in windows], [reset, reset])
        self.assertAlmostEqual(windows[0]["remaining_percent"], 99.07)

    def test_cursor_uses_billing_cycle_only_without_model_pools(self) -> None:
        windows = api._cursor_quota_windows({
            "individualUsage": {"plan": {"enabled": False, "autoPercentUsed": 50}},
            "teamUsage": {"plan": {"totalPercentUsed": 8, "used": 600, "limit": 800}},
        })
        self.assertEqual([(row["label"], row["used_percent"]) for row in windows], [("Billing cycle", 8.0)])
        self.assertEqual(api._cursor_quota_windows({"individualUsage": {"plan": {"used": 600, "limit": 800}}}), [])

    def test_cursor_refuses_to_use_another_accounts_limits(self) -> None:
        with patch.object(api, "_runtime_key", return_value="mock-key"), \
             patch.object(api, "_cursor_dashboard_cookie", return_value="mock-cookie"), \
             patch.object(api, "_get_json", side_effect=[{"userId": 42}, {"id": 43}]) as fetch:
            result = api._probe_cursor()
        self.assertFalse(result["available"])
        self.assertIn("different accounts", result["unavailable_reason"])
        self.assertEqual(fetch.call_count, 2)


if __name__ == "__main__":
    unittest.main()
