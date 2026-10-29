"""Native generator/reference-verifier regressions for PR51 P1-01.

All market data is synthetic. Equal peer dates deliberately do not certify
an exchange calendar, the latest session, or production EOD readiness.
"""
import copy
import json
import shutil

import pytest

from test_deterministic_metric_proof import _seed, proofmod, store

STAGES = ("open_30m", "open_60m", "close")
TRADE_DATE = "2026-09-05"
BEHIND = "provider_affine_daily_series_behind_observed_peer"
UNAVAILABLE = "provider_affine_daily_series_unavailable"


def _stage_seed(tmp_path, stage, *, unpriced=False):
    root, _ = _seed(tmp_path)
    end = {"open_30m": "10:00", "open_60m": "10:30", "close": "16:00"}[stage]
    event = "15:59" if stage == "close" else end
    timestamp = f"{TRADE_DATE}T{event}:00-04:00"
    window = {"start": "15:45" if stage == "close" else "09:30", "end": end}
    facts = [
        {
            "symbol": symbol,
            "asset_class": "stocks" if symbol == "AAA" else "etf",
            "session": "regular",
            "event_time": timestamp,
            "source_timestamp": timestamp,
            "source_contract": "nasdaq_public_chart_last_sale_volume_v1",
            "last_sale": price,
            "reported_volume": 12345.0,
        }
        for symbol, price in (("AAA", 170.0), ("SPY", 470.0), ("QQQ", 580.0))
    ]
    if unpriced:
        facts.append({**facts[0], "symbol": "NOPRICE", "last_sale": None})
    path, blob = store.write_capture(
        root, trade_date=TRADE_DATE, session="regular",
        provider="nasdaq_public_intraday", capture_id=f"identity-{stage}",
        generated_at=timestamp, actual_data_cutoff=timestamp,
        window=window, feed_scope="fixture", qualified_facts=facts,
    )
    store.write_snapshot(
        root, stage=stage, trade_date=TRADE_DATE,
        snapshot_id=f"identity-proof-{stage}", generated_at=timestamp,
        data_refs=[{"path": path, "blob_sha": blob, "kind": "regular_intraday_capture"}],
        coverage={"available": 3}, missing=[], target_window=window,
        actual_data_cutoff=timestamp,
    )
    return root


def _build(root, stage):
    return proofmod.build_proof(
        root, trade_date=TRADE_DATE, stage=stage, data_plane_commit_sha="a" * 40,
    )[0]


def _advance(root, symbols):
    for symbol in symbols:
        store.append_daily_bars(
            root, provider="twelve_data_basic", symbol=symbol,
            asset_class="stocks" if symbol == "AAA" else "etf",
            bars=[{"trade_date": "2026-07-31", "close": 600.0}],
            series_semantics="daily_regular_ohlcv", adjustment_semantics="provider_reported",
        )


def _lagging_proofs(tmp_path, stage):
    root = _stage_seed(tmp_path, stage)
    original = _build(root, stage)
    _advance(root, ("SPY", "QQQ"))
    return root, original, _build(root, stage)


@pytest.mark.parametrize("stage", STAGES)
def test_equal_peer_dates_remain_valid_without_calendar_claim(tmp_path, stage):
    root = _stage_seed(tmp_path, stage)
    proof = _build(root, stage)
    assert {row["symbol"] for row in proof["subjects"]} == {"AAA", "SPY", "QQQ"}
    assert proof["missing"] == []
    proofmod.verify_proof_source_blobs(root, proof)


@pytest.mark.parametrize("stage", STAGES)
def test_lagging_history_is_not_emitted_as_one_session_metric(tmp_path, stage):
    root, _, proof = _lagging_proofs(tmp_path, stage)
    assert "AAA" not in {row["symbol"] for row in proof["subjects"]}
    assert proof["missing"] == [{"symbol": "AAA", "reason": BEHIND}]
    proofmod.verify_proof_source_blobs(root, proof)


@pytest.mark.parametrize("stage", STAGES)
@pytest.mark.parametrize("identity", ("AAA", "aaa", "AaA"))
def test_standalone_verifier_rejects_proof_that_hides_stale_peer(tmp_path, stage, identity):
    root, original, current = _lagging_proofs(tmp_path, stage)
    stale = copy.deepcopy(next(row for row in original["subjects"] if row["symbol"] == "AAA"))
    stale["symbol"] = identity
    forged = copy.deepcopy(current)
    forged["subjects"].append(stale)
    forged["missing"] = []
    with pytest.raises(proofmod.MetricProofError):
        proofmod.verify_proof_source_blobs(root, forged)


@pytest.mark.parametrize("stage", STAGES)
@pytest.mark.parametrize("mutation", ("omit", "wrong_reason", "lowercase", "mixed_case", "duplicate", "overlap", "unselected"))
def test_standalone_verifier_rejects_incorrect_missing_partition(tmp_path, stage, mutation):
    root, _, proof = _lagging_proofs(tmp_path, stage)
    bad = copy.deepcopy(proof)
    if mutation == "omit":
        bad["missing"] = []
    elif mutation == "wrong_reason":
        bad["missing"][0]["reason"] = UNAVAILABLE
    elif mutation == "lowercase":
        bad["missing"][0]["symbol"] = "aaa"
    elif mutation == "mixed_case":
        bad["missing"][0]["symbol"] = "AaA"
    elif mutation == "duplicate":
        bad["missing"].append(copy.deepcopy(bad["missing"][0]))
    elif mutation == "overlap":
        bad["missing"].append({"symbol": "SPY", "reason": BEHIND})
    else:
        bad["missing"].append({"symbol": "UNSELECTED", "reason": UNAVAILABLE})
    with pytest.raises(proofmod.MetricProofError):
        proofmod.verify_proof_source_blobs(root, bad)


@pytest.mark.parametrize("stage", STAGES)
@pytest.mark.parametrize("mutation", ("lowercase", "mixed_case", "duplicate", "omit", "empty_partition", "null_missing"))
def test_standalone_verifier_requires_canonical_complete_subject_partition(tmp_path, stage, mutation):
    root = _stage_seed(tmp_path, stage)
    bad = _build(root, stage)
    if mutation in {"lowercase", "mixed_case"}:
        next(row for row in bad["subjects"] if row["symbol"] == "AAA")["symbol"] = "aaa" if mutation == "lowercase" else "AaA"
    elif mutation == "duplicate":
        bad["subjects"].append(copy.deepcopy(bad["subjects"][0]))
    elif mutation == "omit":
        bad["subjects"].pop()
    elif mutation == "empty_partition":
        bad["subjects"] = []
    else:
        bad["missing"] = None
    with pytest.raises(proofmod.MetricProofError):
        proofmod.verify_proof_source_blobs(root, bad)


@pytest.mark.parametrize("stage", STAGES)
def test_true_daily_unavailability_remains_distinct_from_peer_lag(tmp_path, stage):
    root = _stage_seed(tmp_path, stage)
    shutil.rmtree(root / "series/daily/twelve_data_basic/AAA")
    proof = _build(root, stage)
    assert proof["missing"] == [{"symbol": "AAA", "reason": UNAVAILABLE}]
    proofmod.verify_proof_source_blobs(root, proof)
    proof["missing"][0]["reason"] = BEHIND
    with pytest.raises(proofmod.MetricProofError):
        proofmod.verify_proof_source_blobs(root, proof)


@pytest.mark.parametrize("stage", STAGES)
def test_unqualified_capture_reason_is_verified_independently(tmp_path, stage):
    root = _stage_seed(tmp_path, stage, unpriced=True)
    proof = _build(root, stage)
    assert proof["missing"] == [{"symbol": "NOPRICE", "reason": "qualified_stage_target_unavailable"}]
    proofmod.verify_proof_source_blobs(root, proof)
    proof["missing"][0]["reason"] = UNAVAILABLE
    with pytest.raises(proofmod.MetricProofError):
        proofmod.verify_proof_source_blobs(root, proof)


@pytest.mark.parametrize("stage", STAGES)
@pytest.mark.parametrize("damage", ("index_count", "missing_index", "missing_shard"))
def test_corrupt_history_cannot_be_hidden_by_explicit_missing(tmp_path, stage, damage):
    root, _, proof = _lagging_proofs(tmp_path, stage)
    index_path = root / "series/daily/twelve_data_basic/AAA/_index.json"
    index = json.loads(index_path.read_text())
    if damage == "index_count":
        index["record_count"] += 1
        index_path.write_bytes(store.canonical_bytes(index) + b"\n")
    elif damage == "missing_index":
        index_path.unlink()
    else:
        (index_path.parent / index["shards"][0]["path"]).unlink()
    proof["missing"][0]["reason"] = UNAVAILABLE
    with pytest.raises(proofmod.MetricProofError):
        proofmod.verify_proof_source_blobs(root, proof)


@pytest.mark.parametrize("stage", STAGES)
@pytest.mark.parametrize("benchmark", ("SPY", "QQQ"))
def test_generator_keeps_behind_peer_benchmark_null(tmp_path, stage, benchmark):
    root = _stage_seed(tmp_path, stage)
    _advance(root, {"AAA", "SPY", "QQQ"} - {benchmark})
    proof = _build(root, stage)
    assert proof["missing"] == [{"symbol": benchmark, "reason": BEHIND}]
    assert all(row["benchmark_metrics"][benchmark] is None for row in proof["subjects"])
    proofmod.verify_proof_source_blobs(root, proof)
