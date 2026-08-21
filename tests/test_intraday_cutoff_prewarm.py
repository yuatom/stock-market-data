from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import collect_market_data as entrypoint

ET = ZoneInfo("America/New_York")


class IntradayCutoffPrewarmTest(unittest.TestCase):
    def test_regular_stage_cutoff_waits_only_inside_bounded_prewarm_window(self) -> None:
        self.assertEqual(
            entrypoint.regular_stage_cutoff_wait_seconds(
                mode="open_15m",
                trade_date="2026-08-19",
                now_et=datetime(2026, 8, 19, 9, 40, tzinfo=ET),
            ),
            300,
        )
        self.assertEqual(
            entrypoint.regular_stage_cutoff_wait_seconds(
                mode="open_30m",
                trade_date="2026-08-19",
                now_et=datetime(2026, 8, 19, 9, 55, tzinfo=ET),
            ),
            300,
        )
        self.assertEqual(
            entrypoint.regular_stage_cutoff_wait_seconds(
                mode="open_60m",
                trade_date="2026-08-19",
                now_et=datetime(2026, 8, 19, 10, 25, tzinfo=ET),
            ),
            300,
        )

    def test_regular_stage_cutoff_allows_provider_access_at_or_after_cutoff(self) -> None:
        self.assertEqual(
            entrypoint.regular_stage_cutoff_wait_seconds(
                mode="open_15m",
                trade_date="2026-08-19",
                now_et=datetime(2026, 8, 19, 9, 45, tzinfo=ET),
            ),
            0,
        )
        self.assertEqual(
            entrypoint.regular_stage_cutoff_wait_seconds(
                mode="open_60m",
                trade_date="2026-08-19",
                now_et=datetime(2026, 8, 19, 10, 31, tzinfo=ET),
            ),
            0,
        )

    def test_regular_stage_cutoff_fails_closed_when_requested_too_early(self) -> None:
        with self.assertRaisesRegex(SystemExit, "provider access is too early"):
            entrypoint.regular_stage_cutoff_wait_seconds(
                mode="open_15m",
                trade_date="2026-08-19",
                now_et=datetime(2026, 8, 19, 9, 30, tzinfo=ET),
            )

    def test_regular_stage_cutoff_rejects_future_trade_date_but_allows_historical(self) -> None:
        with self.assertRaisesRegex(SystemExit, "future relative to ET"):
            entrypoint.regular_stage_cutoff_wait_seconds(
                mode="open_15m",
                trade_date="2026-08-20",
                now_et=datetime(2026, 8, 19, 9, 45, tzinfo=ET),
            )
        self.assertEqual(
            entrypoint.regular_stage_cutoff_wait_seconds(
                mode="open_15m",
                trade_date="2026-08-18",
                now_et=datetime(2026, 8, 19, 9, 30, tzinfo=ET),
            ),
            0,
        )

    def test_partial_predecessors_are_reused_without_refetch(self) -> None:
        argv = [
            "--mode",
            "open_60m",
            "--trade-date",
            "2026-08-20",
            "--store-root",
            "unused-store",
        ]
        with mock.patch.object(
            entrypoint.base,
            "_load_prior_snapshot",
            side_effect=[([{"path": "open15"}], ["META"]), ([{"path": "open30"}], ["QQQ"])],
        ), mock.patch.object(entrypoint.runtime, "main") as runtime_main:
            entrypoint._ensure_predecessors(argv)
        runtime_main.assert_not_called()

    def test_absent_predecessor_is_recovered_once(self) -> None:
        argv = [
            "--mode",
            "open_30m",
            "--trade-date",
            "2026-08-20",
            "--store-root",
            "unused-store",
        ]
        with mock.patch.object(
            entrypoint.base,
            "_load_prior_snapshot",
            side_effect=[([], ["all"]), ([{"path": "open15"}], ["META"])],
        ), mock.patch.object(entrypoint.runtime, "main", return_value=0) as runtime_main:
            entrypoint._ensure_predecessors(argv)
        runtime_main.assert_called_once()
        recovered_argv = runtime_main.call_args.args[0]
        self.assertEqual(recovered_argv[recovered_argv.index("--mode") + 1], "open_15m")

    def test_intraday_schedules_prestart_but_runtime_maps_same_stage(self) -> None:
        caller = (ROOT / ".github/workflows/market-data-collector.yml").read_text(encoding="utf-8")
        runtime = (ROOT / ".github/workflows/market-data-collector-runtime.yml").read_text(encoding="utf-8")

        expected = {
            "40 9 * * 1-5": "open_15m",
            "55 9 * * 1-5": "open_30m",
            "25 10 * * 1-5": "open_60m",
        }
        for schedule, mode in expected.items():
            self.assertIn(f"cron: '{schedule}'", caller)
            self.assertIn(f"'{schedule}': '{mode}'", runtime)

        for obsolete in ("46 9 * * 1-5", "1 10 * * 1-5", "31 10 * * 1-5"):
            self.assertNotIn(obsolete, caller)
            self.assertNotIn(obsolete, runtime)
        self.assertIn("timeout-minutes: 12", runtime)


if __name__ == "__main__":
    unittest.main()
