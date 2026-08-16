import json
from pathlib import Path
import sys
import unittest


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

    def test_every_context_proxy_is_daily_and_intraday(self):
        daily = {symbol for symbol, _asset in collection.daily_universe(self.universe)}
        intraday = {symbol for symbol, _asset in collection.intraday_universe(self.universe)}
        for symbol in collection.context_symbols(self.universe):
            self.assertIn(symbol, daily)
            self.assertIn(symbol, intraday)

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

    def test_live_runtime_has_no_research_watchlist_cli_dependency(self):
        source = (ROOT / "scripts/market_data_collection.py").read_text(encoding="utf-8")
        self.assertNotIn('parser.add_argument("--watchlist"', source)
        self.assertNotIn('parser.add_argument("--data-completeness"', source)
        self.assertIn('parser.add_argument("--universe"', source)

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
            {"daily_baseline_init", "historical_context_repair"},
        )
        text = json.dumps(schema, sort_keys=True)
        self.assertIn("cutoff_valid_cross_asset_context_proxies_only", text)
        self.assertIn("owner_authorized_data_plane_maintenance", text)

    def test_live_workflow_uses_first_class_universe(self):
        workflow = (ROOT / ".github/workflows/market-data-collector.yml").read_text(encoding="utf-8")
        self.assertIn("--universe config/collection-universe.json", workflow)
        self.assertIn("maintenance-requests", workflow)
        self.assertIn("historical_context_repair", workflow)
        self.assertIn("runtime_compatibility_file_dependency_for_live_collection", (ROOT / "config/data-plane.yaml").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
