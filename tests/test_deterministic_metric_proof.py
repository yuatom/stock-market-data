import importlib.util
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


store = _load("metric_proof_store", "scripts/market_data_store.py")
proofmod = _load("metric_proof_builder", "scripts/build_deterministic_metric_proof.py")


def _bars(base: float, count: int = 60):
    start = date(2026, 6, 1)
    return [
        {
            "trade_date": (start + timedelta(days=i)).isoformat(),
            "open": base + i - 0.5,
            "high": base + i + 1.0,
            "low": base + i - 1.0,
            "close": base + i,
            "volume": 1000.0 + i,
        }
        for i in range(count)
    ]


def _seed(tmp_path: Path):
    root = tmp_path / "data" / "market-data"
    for symbol, base in (("AAA", 100.0), ("SPY", 400.0), ("QQQ", 500.0)):
        store.append_daily_bars(
            root,
            provider="twelve_data_basic",
            symbol=symbol,
            asset_class="stocks" if symbol == "AAA" else "etf",
            bars=_bars(base),
            series_semantics="daily_regular_ohlcv",
            adjustment_semantics="provider_reported",
        )
    generated = "2026-09-05T10:00:00-04:00"
    facts = []
    for symbol, price in (("AAA", 170.0), ("SPY", 470.0), ("QQQ", 580.0)):
        facts.append(
            {
                "symbol": symbol,
                "asset_class": "stocks" if symbol == "AAA" else "etf",
                "session": "regular",
                "event_time": generated,
                "source_timestamp": generated,
                "last_sale": price,
                "reported_volume": 12345.0,
            }
        )
    capture_path, capture_blob = store.write_capture(
        root,
        trade_date="2026-09-05",
        session="regular",
        provider="nasdaq_public_intraday",
        capture_id="open30-fixture",
        generated_at=generated,
        actual_data_cutoff=generated,
        window={"start": "09:30", "end": "10:00"},
        feed_scope="fixture",
        qualified_facts=facts,
    )
    store.write_snapshot(
        root,
        stage="open_30m",
        trade_date="2026-09-05",
        snapshot_id="open30-proof-fixture",
        generated_at=generated,
        data_refs=[{"path": capture_path, "blob_sha": capture_blob, "kind": "regular_intraday_capture"}],
        coverage={"available": 3},
        missing=[],
        target_window={"start": "09:30", "end": "10:00"},
        actual_data_cutoff=generated,
    )
    return root, capture_path


class DeterministicMetricProofTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_proof_matches_stock_dairy_formula_semantics_and_binds_exact_blobs(self):
        root, _ = _seed(self.tmp_path)
        proof, rel = proofmod.build_proof(
            root,
            trade_date="2026-09-05",
            stage="open_30m",
            data_plane_commit_sha="a" * 40,
        )
        self.assertTrue(rel.endswith("/open30-proof-fixture.json"))
        aaa = next(item for item in proof["subjects"] if item["symbol"] == "AAA")
        self.assertEqual(aaa["metrics"]["returns_pct"]["1session"], round((170.0 / 159.0 - 1.0) * 100.0, 6))
        self.assertEqual(aaa["metrics"]["returns_pct"]["3session"], round((170.0 / 157.0 - 1.0) * 100.0, 6))
        self.assertEqual(aaa["metrics"]["moving_averages"]["ma20"], round(sum(float(x) for x in range(140, 160)) / 20.0, 6))
        self.assertEqual(aaa["metrics"]["volume"]["status"], "unavailable")
        self.assertEqual(aaa["benchmark_metrics"]["SPY"]["status"], "available")
        self.assertEqual(aaa["benchmark_metrics"]["QQQ"]["status"], "available")
        self.assertEqual(len(aaa["daily_series"]["index_blob_sha"]), 40)
        self.assertTrue(aaa["daily_series"]["shards"])
        self.assertEqual(len(aaa["target"]["capture_blob_sha"]), 40)
        self.assertEqual(proof["missing"], [])

    def test_proof_rejects_tampered_snapshot_capture_blob(self):
        root, capture_path = _seed(self.tmp_path)
        path = root / capture_path
        value = json.loads(path.read_text(encoding="utf-8"))
        value["qualified_facts"][0]["last_sale"] = 999.0
        path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(proofmod.MetricProofError, "blob mismatch"):
            proofmod.build_proof(root, trade_date="2026-09-05", stage="open_30m", data_plane_commit_sha="a" * 40)

    def test_missing_daily_series_is_explicit_not_fabricated(self):
        root, _ = _seed(self.tmp_path)
        aaa_dir = root / "series" / "daily" / "twelve_data_basic" / "AAA"
        for path in sorted(aaa_dir.glob("*")):
            path.unlink()
        aaa_dir.rmdir()
        proof, _ = proofmod.build_proof(root, trade_date="2026-09-05", stage="open_30m", data_plane_commit_sha="b" * 40)
        self.assertEqual({item["symbol"] for item in proof["subjects"]}, {"QQQ", "SPY"})
        self.assertEqual({item["symbol"] for item in proof["missing"]}, {"AAA"})

    def test_builder_has_no_network_or_provider_fetch_surface(self):
        text = (ROOT / "scripts/build_deterministic_metric_proof.py").read_text(encoding="utf-8")
        for forbidden in ("urllib", "requests.", "TWELVE_DATA_API_KEY", "urlopen", "fetch_"):
            self.assertNotIn(forbidden, text)

    def test_schema_requires_exact_identity_and_missing_accounting(self):
        schema = json.loads((ROOT / "schemas/deterministic-metric-proof.schema.json").read_text(encoding="utf-8"))
        self.assertIn("snapshot", schema["required"])
        self.assertIn("subjects", schema["required"])
        self.assertIn("missing", schema["required"])
        self.assertEqual(schema["properties"]["data_plane_commit_sha"]["pattern"], "^[0-9a-f]{40}$")


if __name__ == "__main__":
    unittest.main()
