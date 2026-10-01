import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import collection_universe as collection  # noqa: E402
import market_data_collection as runtime  # noqa: E402


class CollectionUniverseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path = ROOT / "config/collection-universe.json"
        cls.universe = collection.load_collection_universe(cls.path)

    def test_context_baseline_has_exact_supported_categories(self):
        proxies = collection.context_proxies(self.universe)
        self.assertEqual(set(proxies), set(collection.REQUIRED_CONTEXT_CATEGORIES))
        self.assertEqual(
            collection.context_symbols(self.universe),
            ["GLD", "IBIT", "TLT", "UUP", "VIXY"],
        )

    def test_close_supported_baseline_maintenance_is_derived_from_groups(self):
        self.assertEqual(
            collection.sector_symbols(self.universe),
            ["XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY"],
        )
        self.assertEqual(
            collection.close_supported_baseline_symbols(self.universe),
            ["GLD", "IBIT", "TLT", "UUP", "VIXY", "XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY"],
        )
        live = {symbol for symbol, _asset in collection.intraday_universe(self.universe)}
        self.assertFalse(set(collection.sector_symbols(self.universe)) & live)

    def test_historical_repair_executes_exact_supported_baseline_universe(self):
        expected = collection.close_supported_baseline_universe(self.universe)
        with mock.patch.object(runtime, "collect_regular_window", return_value={"mode": "historical_context_repair", "status": "ok"}) as collect:
            rc = runtime.main(
                [
                    "--mode",
                    "historical_context_repair",
                    "--trade-date",
                    "2026-08-14",
                    "--universe",
                    str(self.path),
                    "--config",
                    str(ROOT / "config/market-data-store.yaml"),
                    "--access-config",
                    str(ROOT / "config/market-data-collector-access.yaml"),
                    "--maintenance-authorized",
                ]
            )
        self.assertEqual(rc, 0)
        kwargs = collect.call_args.kwargs
        self.assertIsNone(kwargs.get("eligible_universe"))
        self.assertEqual(kwargs["symbols_override"], [symbol for symbol, _asset in expected])
        self.assertEqual(kwargs["stage"], "close")
        self.assertEqual(kwargs["start_et"], "15:45")
        self.assertEqual(kwargs["end_et"], "16:00")

    def test_historical_repair_real_config_preserves_full_scope_and_bounded_requests(self):
        maintenance = collection.close_supported_baseline_universe(self.universe)
        requested = [symbol for symbol, _asset in maintenance]
        intraday = dict(collection.intraday_universe(self.universe))
        expected_full = sorted(set(intraday) | set(requested))
        # Exercise real canonical groups: sector ETFs are outside intraday.
        self.assertTrue(set(requested) - set(intraday))
        no_rows = ([], {"raw_row_count": 0})
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(runtime, "fetch_nasdaq_regular", return_value=no_rows) as nasdaq, \
             mock.patch.object(runtime, "fetch_twelve_regular", return_value=no_rows) as twelve, \
             mock.patch.object(runtime.base, "_load_prior_snapshot", return_value=([], [])):
            result = runtime.collect_regular_window(
                mode=runtime.HISTORICAL_CONTEXT_REPAIR, stage="close",
                trade_date="2026-09-30", start_et="15:45", end_et="16:00",
                store_root=Path(tmp), universe_config=self.universe, config={},
                access={"nasdaq_public_intraday": {"max_workers": 1}},
                symbols_override=requested,
            )
        self.assertEqual(sorted(call.args[0] for call in nasdaq.call_args_list), requested)
        self.assertEqual(sorted(call.args[0] for call in twelve.call_args_list), requested)
        self.assertEqual(result["coverage"]["increment"]["requested_symbol_count"], len(requested))
        self.assertEqual(result["coverage"]["stage_snapshot"]["requested_symbol_count"], len(expected_full))
        self.assertEqual(result["snapshot_missing"], expected_full)
        self.assertEqual(result["status"], "no_new_qualified_facts")
        self.assertFalse(result["snapshot_written"])

    def test_every_context_proxy_is_daily_and_intraday(self):
        daily = {symbol for symbol, _asset in collection.daily_universe(self.universe)}
        intraday = {symbol for symbol, _asset in collection.intraday_universe(self.universe)}
        for symbol in collection.context_symbols(self.universe):
            self.assertIn(symbol, daily)
            self.assertIn(symbol, intraday)

    def test_every_close_supported_baseline_symbol_is_daily(self):
        daily = {symbol for symbol, _asset in collection.daily_universe(self.universe)}
        for symbol in collection.close_supported_baseline_symbols(self.universe):
            self.assertIn(symbol, daily)

    def test_proxy_fact_keeps_non_equivalence_guard(self):
        fact = collection.decorate_fact(
            self.universe,
            {
                "symbol": "TLT",
                "asset_class": "etf",
                "session": "regular",
                "event_time": "2026-08-14T15:59:00-04:00",
                "source_timestamp": "2026-08-14T15:59:00-04:00",
                "last_sale": 100.0,
            },
        )
        context = fact["market_context"]
        self.assertEqual(context["category"], "rates")
        self.assertEqual(context["quality_role"], collection.CONTEXT_PROXY_ROLE)
        self.assertIn("official_yield_curve", context["not_equivalent_to"])
        self.assertIn("treasury_yield_level", context["not_equivalent_to"])

    def test_runtime_and_baseline_tools_share_one_universe_cli(self):
        for relative in (
            "scripts/market_data_collection.py",
            "scripts/initialize_market_data_baseline.py",
            "scripts/rebase_market_data_baseline.py",
        ):
            source = (ROOT / relative).read_text(encoding="utf-8")
            self.assertNotIn('parser.add_argument("--watchlist"', source, relative)
            self.assertNotIn('parser.add_argument("--data-completeness"', source, relative)
            self.assertIn('parser.add_argument("--universe"', source, relative)

    def test_historical_context_repair_requires_explicit_authorization(self):
        with self.assertRaisesRegex(RuntimeError, "requires --maintenance-authorized"):
            runtime.main(
                [
                    "--mode",
                    "historical_context_repair",
                    "--trade-date",
                    "2026-08-14",
                    "--universe",
                    str(self.path),
                    "--config",
                    str(ROOT / "config/market-data-store.yaml"),
                    "--access-config",
                    str(ROOT / "config/market-data-collector-access.yaml"),
                ]
            )

    def test_maintenance_request_schema_binds_mode_to_scope(self):
        schema = json.loads(
            (ROOT / "schemas/market-data-maintenance-request.schema.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            set(schema["properties"]["mode"]["enum"]),
            {"daily_baseline_init", "historical_context_repair", "premarket_probe_replay"},
        )
        text = json.dumps(schema, sort_keys=True)
        self.assertIn("cutoff_valid_close_supported_baseline", text)
        self.assertIn("historical_premarket_probe_sample", text)
        self.assertIn("probe_blob_sha", text)
        self.assertNotIn("cutoff_valid_cross_asset_context_proxies_only", text)
        self.assertIn("owner_authorized_data_plane_maintenance", text)

    def test_workflow_and_contract_forbid_runtime_compat_membership_files(self):
        workflow = (ROOT / ".github/workflows/market-data-collector-runtime.yml").read_text(encoding="utf-8")
        contract = (ROOT / "config/data-plane.yaml").read_text(encoding="utf-8")
        self.assertIn("--universe config/collection-universe.json", workflow)
        self.assertIn("maintenance-requests", workflow)
        self.assertIn("historical_context_repair", workflow)
        self.assertIn("cutoff_valid_close_supported_baseline", workflow)
        self.assertIn("sector_symbols_requested", workflow)
        self.assertNotIn("build_collector_compat.py", workflow)
        self.assertNotIn("collector-watchlist.json", workflow)
        self.assertNotIn("collector-completeness.yaml", workflow)
        self.assertIn("runtime_compatibility_file_dependency: false", contract)
        self.assertIn("generated_compatibility_membership_files_forbidden: true", contract)
        self.assertFalse((ROOT / "scripts/build_collector_compat.py").exists())


if __name__ == "__main__":
    unittest.main()
