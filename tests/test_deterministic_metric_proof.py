import copy
import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


store = _load("metric_proof_store", "scripts/market_data_store.py")
sys.modules["market_data_store"] = store
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


def _seed(tmp_path: Path, *, spy_event_time=None):
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
        event_time = spy_event_time if symbol == "SPY" and spy_event_time else generated
        facts.append(
            {
                "symbol": symbol,
                "asset_class": "stocks" if symbol == "AAA" else "etf",
                "session": "regular",
                "event_time": event_time,
                "source_timestamp": event_time,
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


def _materialize_proof(root: Path, data_plane_commit_sha: str):
    proof, rel = proofmod.build_proof(
        root,
        trade_date="2026-09-05",
        stage="open_30m",
        data_plane_commit_sha=data_plane_commit_sha,
    )
    blob = proofmod._write_json(root / rel, proof)
    pointer = proofmod._proof_pointer(proof, rel=rel, blob=blob)
    latest = root / "proofs/deterministic-metrics/2026-09/2026-09-05/open_30m/latest.json"
    latest.parent.mkdir(parents=True, exist_ok=True)
    latest.write_bytes(proofmod.canonical_bytes(pointer) + b"\n")
    return proof, rel


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
        proofmod.verify_proof_source_blobs(root, proof)

    def test_proof_rejects_tampered_snapshot_capture_blob(self):
        root, capture_path = _seed(self.tmp_path)
        path = root / capture_path
        value = json.loads(path.read_text(encoding="utf-8"))
        value["qualified_facts"][0]["last_sale"] = 999.0
        path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(proofmod.MetricProofError, "blob mismatch"):
            proofmod.build_proof(root, trade_date="2026-09-05", stage="open_30m", data_plane_commit_sha="a" * 40)

    def test_proof_rejects_tampered_declared_daily_series_shard(self):
        root, _ = _seed(self.tmp_path)
        index_path = root / "series" / "daily" / "twelve_data_basic" / "AAA" / "_index.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        shard_path = index_path.parent / index["shards"][0]["path"]
        value = json.loads(shard_path.read_text(encoding="utf-8"))
        value["records"][0]["close"] = 9999.0
        shard_path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(proofmod.MetricProofError, "daily-series canonical integrity failure"):
            proofmod.build_proof(root, trade_date="2026-09-05", stage="open_30m", data_plane_commit_sha="a" * 40)

    def test_daily_series_canonical_metadata_corruption_fails_closed(self):
        cases = {
            "start": lambda value: int(value) + 1,
            "end": lambda value: int(value) + 1,
            "count": lambda value: int(value) + 1,
            "byte_length": lambda value: int(value) + 1,
            "json_sha256": lambda _value: "0" * 64,
            "record_count": lambda value: int(value) + 1,
        }
        for field, mutate in cases.items():
            with self.subTest(field=field):
                root, _ = _seed(self.tmp_path / field)
                index_path = root / "series" / "daily" / "twelve_data_basic" / "AAA" / "_index.json"
                index = json.loads(index_path.read_text(encoding="utf-8"))
                if field == "record_count":
                    index[field] = mutate(index[field])
                else:
                    index["shards"][0][field] = mutate(index["shards"][0][field])
                index_path.write_bytes(store.canonical_bytes(index) + b"\n")
                with self.assertRaisesRegex(proofmod.MetricProofError, "daily-series canonical integrity failure"):
                    proofmod.build_proof(
                        root,
                        trade_date="2026-09-05",
                        stage="open_30m",
                        data_plane_commit_sha="1" * 40,
                    )

    def test_missing_daily_series_is_explicit_not_fabricated(self):
        root, _ = _seed(self.tmp_path)
        aaa_dir = root / "series" / "daily" / "twelve_data_basic" / "AAA"
        for path in sorted(aaa_dir.glob("*")):
            path.unlink()
        aaa_dir.rmdir()
        proof, _ = proofmod.build_proof(root, trade_date="2026-09-05", stage="open_30m", data_plane_commit_sha="b" * 40)
        self.assertEqual({item["symbol"] for item in proof["subjects"]}, {"QQQ", "SPY"})
        self.assertEqual({item["symbol"] for item in proof["missing"]}, {"AAA"})

    def test_intraday_relative_strength_requires_exact_target_timestamp_alignment(self):
        root, _ = _seed(self.tmp_path, spy_event_time="2026-09-05T09:59:00-04:00")
        proof, _ = proofmod.build_proof(root, trade_date="2026-09-05", stage="open_30m", data_plane_commit_sha="c" * 40)
        aaa = next(item for item in proof["subjects"] if item["symbol"] == "AAA")
        self.assertIsNone(aaa["benchmark_metrics"]["SPY"])
        self.assertEqual(aaa["benchmark_metrics"]["QQQ"]["status"], "available")

    def test_reference_verifier_rejects_sources_from_a_different_store_tree(self):
        source_root, _ = _seed(self.tmp_path / "source")
        proof, _ = proofmod.build_proof(
            source_root,
            trade_date="2026-09-05",
            stage="open_30m",
            data_plane_commit_sha="d" * 40,
        )
        other_root = self.tmp_path / "other" / "data" / "market-data"
        shutil.copytree(source_root, other_root)
        index_path = other_root / "series" / "daily" / "twelve_data_basic" / "AAA" / "_index.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        shard_path = index_path.parent / index["shards"][0]["path"]
        value = json.loads(shard_path.read_text(encoding="utf-8"))
        value["records"][0]["close"] = 1234.0
        shard_path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(proofmod.MetricProofError, "daily-series canonical integrity failure"):
            proofmod.verify_proof_source_blobs(other_root, proof)

    def test_reference_verifier_rejects_valid_capture_not_in_snapshot(self):
        root, _ = _seed(self.tmp_path)
        proof, _ = proofmod.build_proof(root, trade_date="2026-09-05", stage="open_30m", data_plane_commit_sha="2" * 40)
        event_time = "2026-09-05T10:00:00-04:00"
        capture_path, capture_blob = store.write_capture(
            root,
            trade_date="2026-09-05",
            session="regular",
            provider="nasdaq_public_intraday",
            capture_id="unbound-capture",
            generated_at=event_time,
            actual_data_cutoff=event_time,
            window={"start": "09:30", "end": "10:00"},
            feed_scope="fixture",
            qualified_facts=[{
                "symbol": "AAA",
                "asset_class": "stocks",
                "session": "regular",
                "event_time": event_time,
                "source_timestamp": event_time,
                "last_sale": 170.0,
                "reported_volume": 12345.0,
            }],
        )
        bad = copy.deepcopy(proof)
        aaa = next(item for item in bad["subjects"] if item["symbol"] == "AAA")
        aaa["target"].update({"capture_path": capture_path, "capture_blob_sha": capture_blob})
        with self.assertRaisesRegex(proofmod.MetricProofError, "not the snapshot-selected observation"):
            proofmod.verify_proof_source_blobs(root, bad)

    def test_reference_verifier_rejects_stale_snapshot_referenced_observation(self):
        root, current_capture_path = _seed(self.tmp_path)
        current_capture_blob = proofmod.git_blob_sha_bytes((root / current_capture_path).read_bytes())
        old_time = "2026-09-05T09:59:00-04:00"
        old_path, old_blob = store.write_capture(
            root,
            trade_date="2026-09-05",
            session="regular",
            provider="nasdaq_public_intraday",
            capture_id="open30-stale-fixture",
            generated_at=old_time,
            actual_data_cutoff=old_time,
            window={"start": "09:30", "end": "10:00"},
            feed_scope="fixture",
            qualified_facts=[{
                "symbol": "AAA",
                "asset_class": "stocks",
                "session": "regular",
                "event_time": old_time,
                "source_timestamp": old_time,
                "last_sale": 169.0,
                "reported_volume": 12000.0,
            }],
        )
        store.write_snapshot(
            root,
            stage="open_30m",
            trade_date="2026-09-05",
            snapshot_id="open30-proof-with-history",
            generated_at="2026-09-05T10:01:00-04:00",
            data_refs=[
                {"path": old_path, "blob_sha": old_blob, "kind": "regular_intraday_capture"},
                {"path": current_capture_path, "blob_sha": current_capture_blob, "kind": "regular_intraday_capture"},
            ],
            coverage={"available": 3},
            missing=[],
            target_window={"start": "09:30", "end": "10:00"},
            actual_data_cutoff="2026-09-05T10:00:00-04:00",
        )
        proof, _ = proofmod.build_proof(root, trade_date="2026-09-05", stage="open_30m", data_plane_commit_sha="3" * 40)
        bad = copy.deepcopy(proof)
        aaa = next(item for item in bad["subjects"] if item["symbol"] == "AAA")
        aaa["target"] = {
            "capture_path": old_path,
            "capture_blob_sha": old_blob,
            "event_time": old_time,
            "last_sale": 169.0,
        }
        with self.assertRaisesRegex(proofmod.MetricProofError, "not the snapshot-selected observation"):
            proofmod.verify_proof_source_blobs(root, bad)

    def test_final_tree_verifier_rejects_source_advance_after_proof_materialization(self):
        root, _ = _seed(self.tmp_path)
        sha = "e" * 40
        _materialize_proof(root, sha)
        store.append_daily_bars(
            root,
            provider="twelve_data_basic",
            symbol="AAA",
            asset_class="stocks",
            bars=[{
                "trade_date": "2026-08-01",
                "open": 160.5,
                "high": 162.0,
                "low": 160.0,
                "close": 161.0,
                "volume": 2000.0,
            }],
            series_semantics="daily_regular_ohlcv",
            adjustment_semantics="provider_reported",
        )
        with self.assertRaisesRegex(proofmod.MetricProofError, "persisted proof does not match the final Store tree"):
            proofmod.verify_existing_proof(
                root,
                trade_date="2026-09-05",
                stage="open_30m",
                data_plane_commit_sha=sha,
            )

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

    def test_contract_declares_final_tree_snapshot_and_canonical_series_closure(self):
        import yaml

        contract = yaml.safe_load((ROOT / "config/deterministic-metric-proof.yaml").read_text(encoding="utf-8"))
        source = contract["source_identity"]
        self.assertTrue(source["consumer_verification_must_use_one_exact_pinned_store_tree"])
        self.assertTrue(source["source_blob_mismatch_or_tamper_is_integrity_failure_not_unavailable"])
        self.assertTrue(source["store_publication_must_be_exact_base_cas_bound"])
        self.assertTrue(source["post_proof_remote_rebase_or_merge_forbidden"])
        self.assertTrue(source["target_capture_must_be_exact_member_of_verified_snapshot_data_refs"])
        self.assertTrue(source["target_observation_must_match_snapshot_wide_selected_observation"])
        self.assertTrue(source["daily_series_integrity_must_reuse_canonical_store_reader"])
        self.assertTrue(contract["formulas"]["benchmark_target_event_time_must_equal_subject_target_event_time_for_intraday_rs"])
        self.assertEqual(
            source["executable_reference_verifier"],
            "scripts/build_deterministic_metric_proof.py#verify_proof_source_blobs",
        )
        self.assertEqual(
            source["final_tree_verifier"],
            "scripts/build_deterministic_metric_proof.py#verify_existing_proof",
        )


if __name__ == "__main__":
    unittest.main()
