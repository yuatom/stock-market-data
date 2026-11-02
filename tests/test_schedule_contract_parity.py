"""The collector's scheduled authority and runtime contract must not drift."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


class CollectorScheduleContractParityTest(unittest.TestCase):
    def setUp(self):
        self.workflow = (ROOT / ".github/workflows/market-data-collector.yml").read_text()
        self.runtime = (ROOT / ".github/workflows/market-data-collector-runtime.yml").read_text()
        self.store = yaml.safe_load((ROOT / "config/market-data-store.yaml").read_text())
        self.data_plane = yaml.safe_load((ROOT / "config/data-plane.yaml").read_text())

    def test_schedule_selector_routes_cover_all_declared_crons(self):
        crons = re.findall(r"cron: '([^']+)'", self.workflow)
        schedule_block = self.runtime.split("schedules = {", 1)[1].split("}", 1)[0]
        selector = dict(re.findall(r"^\s*'([^']+)': '([^']+)',?$", schedule_block, re.M))
        self.assertEqual(set(crons), set(selector))
        self.assertEqual(len(crons), 11)
        self.assertEqual(selector["20 7 * * 1-5"], "premarket")
        self.assertEqual(selector["10 7 * * 1-5"], "nasdaq_extended_probe")
        for mode in ("open_15m", "open_30m", "open_60m"):
            self.assertIn(mode, selector.values())

    def test_store_schedule_projection_matches_selector_and_explicit_prewarm(self):
        schedules = self.store["collector"]["schedules_et"]
        mapping = {
            "previous_session_eod": "07:05",
            "premarket_shadow_probe": "07:10",
            "premarket_live": "07:20",
            "open_15m": "09:40",
            "open_30m": "09:55",
            "open_60m": "10:25",
            "close": "16:06",
            "close_retry_1": "16:21",
            "close_retry_2": "16:36",
            "after_hours_shadow_probe": "16:45",
            "close_final": "16:51",
        }
        for key, clock in mapping.items():
            with self.subTest(key=key):
                self.assertEqual(schedules[key], clock)
                minute, hour = clock.split(":")[1], int(clock.split(":")[0])
                self.assertIn(f"{int(minute)} {hour} * * 1-5", self.workflow)
        self.assertEqual(
            [schedules[f"open_{n}m_semantic_cutoff"] for n in (15, 30, 60)],
            ["09:45", "10:00", "10:30"],
        )
        self.assertTrue(self.store["collector"]["scheduling"]["live_premarket_scheduled"])
        self.assertEqual(self.store["collector"]["timeout_minutes"], 12)

    def test_premarket_on_demand_contract_is_exactly_reconciled(self):
        modes = self.store["collector"]["on_demand_request"]["accepted_modes"]
        self.assertEqual(modes, self.data_plane["collector_request_interface"]["runtime_modes"])
        self.assertEqual(modes, ["premarket", "open_15m", "open_30m", "open_60m", "close"])
        self.assertIn("request.get('mode') not in {'premarket','open_15m'", self.runtime)

    def test_premarket_daily_series_is_not_gated_by_eod_schedule(self):
        scheduling = self.store["collector"]["scheduling"]
        daily = self.store["collector"]["twelve_data_daily"]
        eod = self.store["collector"]["schedules_et"]["previous_session_eod"]

        # Fixed 19:30 Asia/Shanghai consumer schedule in stock-dairy@ede8ac0:
        # 06:30 ET in winter, 07:30 ET in summer.
        premarket_report_et = {"us_standard": "06:30", "us_daylight": "07:30"}
        minutes = lambda value: int(value[:2]) * 60 + int(value[3:])

        self.assertEqual(daily["settlement_maturity"]["minimum_next_day_et"], "07:00")
        self.assertEqual(eod, "07:05")
        self.assertGreater(minutes(eod), minutes(premarket_report_et["us_standard"]))
        self.assertLess(minutes(eod), minutes(premarket_report_et["us_daylight"]))

        self.assertFalse(scheduling["daily_series_maturity_schedule_must_finish_before_premarket_report"])
        self.assertTrue(scheduling["scheduled_collector_is_opportunistic_not_report_sla"])
        self.assertTrue(scheduling["on_demand_push_is_primary_live_stage_handoff_when_snapshot_missing"])
        self.assertFalse(scheduling["report_schedule_shift_required"])

        daily_policy = scheduling["premarket_daily_series_policy"]
        self.assertTrue(daily_policy["may_use_existing_qualified_previous_session_baseline"])
        self.assertFalse(daily_policy["missing_or_unqualified_history_blocks_independent_observation"])

    def test_schedule_lateness_is_observed_without_asserting_github_created_at(self):
        self.assertIn("schedule_delivery_observation", self.runtime)
        self.assertIn("nominal_to_invocation_observed_seconds", self.runtime)
        self.assertIn("not_actions_creation", self.runtime)
        self.assertNotIn("'github_workflow_created_at':", self.runtime)
        self.assertIn("queue: max", self.runtime)


if __name__ == "__main__":
    unittest.main()
