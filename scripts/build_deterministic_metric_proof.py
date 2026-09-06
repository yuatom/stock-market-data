#!/usr/bin/env python3
"""Build deterministic stock-dairy metric proofs from exact private-Store bytes.

This module owns arithmetic/proof generation only. It never fetches providers,
never writes research/report state, and never classifies Direction/Risk/Action.
The proof binds every operand to immutable Store blob identities so a no-script
consumer can verify the persisted proof through one pinned Store read SHA.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import market_data_store as store_integrity

HEX40 = re.compile(r"^[0-9a-f]{40}$")
PROVIDER = "twelve_data_basic"
FORMULA_CONTRACT = "stock_dairy_calculation_policy_v2"


class MetricProofError(RuntimeError):
    pass


class MetricProofUnavailable(RuntimeError):
    """The required provider-affine series does not exist at all."""


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def git_blob_sha_bytes(data: bytes) -> str:
    return hashlib.sha1(f"blob {len(data)}\0".encode("ascii") + data).hexdigest()


def _write_json(path: Path, value: Any) -> str:
    payload = canonical_bytes(value) + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = path.read_bytes()
        if existing != payload:
            raise MetricProofError(f"immutable proof conflict: {path}")
    else:
        path.write_bytes(payload)
    return git_blob_sha_bytes(payload)


def _read_json_exact(root: Path, rel: str, expected_blob_sha: str | None = None) -> tuple[dict[str, Any], str]:
    path = root / rel
    raw = path.read_bytes()
    actual = git_blob_sha_bytes(raw)
    if expected_blob_sha is not None and actual != expected_blob_sha:
        raise MetricProofError(f"blob mismatch {rel}: {actual} != {expected_blob_sha}")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise MetricProofError(f"expected JSON object: {rel}")
    return value, actual


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MetricProofError(f"{field} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise MetricProofError(f"{field} must be finite")
    return result


def _pct(current: float, baseline: float) -> float:
    if baseline <= 0:
        raise MetricProofError("percentage baseline must be positive")
    return round((current / baseline - 1.0) * 100.0, 6)


def _mean(values: Iterable[float]) -> float:
    values = list(values)
    if not values:
        raise MetricProofError("mean requires values")
    return round(sum(values) / len(values), 6)


def _canonical_daily_records(
    root: Path,
    *,
    index_rel: str,
    index: Mapping[str, Any],
    symbol: str,
) -> list[dict[str, Any]]:
    """Reuse the canonical Store Daily-Series integrity implementation."""
    try:
        return store_integrity._read_all_records(root / Path(index_rel).parent, index)
    except Exception as exc:
        raise MetricProofError(f"daily-series canonical integrity failure: {symbol}: {exc}") from exc


def _exact_shard_refs(root: Path, *, index_rel: str, index: Mapping[str, Any], symbol: str) -> list[dict[str, str]]:
    shards = index.get("shards") or []
    if not isinstance(shards, list):
        raise MetricProofError(f"series shard metadata must be a list: {symbol}")
    series_dir = Path(index_rel).parent
    refs: list[dict[str, str]] = []
    for meta in shards:
        if not isinstance(meta, Mapping):
            raise MetricProofError(f"series shard metadata must be an object: {symbol}")
        path_name = str(meta.get("path") or "")
        blob = str(meta.get("blob_sha") or "")
        if not path_name or not HEX40.fullmatch(blob):
            raise MetricProofError(f"series index shard identity is incomplete: {symbol}")
        rel = str(series_dir / path_name)
        try:
            _read_json_exact(root, rel, blob)
        except FileNotFoundError as exc:
            raise MetricProofError(f"declared series shard missing: {rel}") from exc
        refs.append({"path": rel, "blob_sha": blob})
    return refs


def _load_daily_series(root: Path, symbol: str, target_trade_date: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    index_rel = f"series/daily/{PROVIDER}/{symbol.upper()}/_index.json"
    try:
        index, index_blob = _read_json_exact(root, index_rel)
    except FileNotFoundError as exc:
        raise MetricProofUnavailable(f"provider-affine daily series unavailable: {symbol}") from exc
    if index.get("provider") != PROVIDER or index.get("symbol") != symbol.upper():
        raise MetricProofError(f"series affinity mismatch: {symbol}")
    records = _canonical_daily_records(root, index_rel=index_rel, index=index, symbol=symbol)
    shard_refs = _exact_shard_refs(root, index_rel=index_rel, index=index, symbol=symbol)
    prior = [row for row in records if str(row.get("trade_date")) < target_trade_date]
    if not prior:
        raise MetricProofError(f"no prior daily records: {symbol}")
    meta = {
        "provider": PROVIDER,
        "index_path": index_rel,
        "index_blob_sha": index_blob,
        "shards": shard_refs,
        "prior_record_count": len(prior),
        "prior_last_trade_date": str(prior[-1]["trade_date"]),
    }
    return prior, meta


def _validated_records(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    previous = None
    for raw in records:
        trade_date = str(raw.get("trade_date") or "")
        if not trade_date or (previous is not None and trade_date <= previous):
            raise MetricProofError("prior records must be strictly ascending")
        close = _number(raw.get("close"), f"close[{trade_date}]")
        if close <= 0:
            raise MetricProofError(f"close[{trade_date}] must be positive")
        row: dict[str, Any] = {"trade_date": trade_date, "close": close}
        if raw.get("volume") is not None:
            volume = _number(raw.get("volume"), f"volume[{trade_date}]")
            if volume < 0:
                raise MetricProofError("volume must be non-negative")
            row["volume"] = volume
        out.append(row)
        previous = trade_date
    return out


def compute_metrics(
    prior_records: Sequence[Mapping[str, Any]],
    *,
    target_trade_date: str,
    target_close: float,
    benchmark_prior_records: Sequence[Mapping[str, Any]] | None = None,
    benchmark_target_close: float | None = None,
) -> dict[str, Any]:
    records = _validated_records(prior_records)
    target_close = _number(target_close, "target_close")
    if target_close <= 0 or records[-1]["trade_date"] >= target_trade_date:
        raise MetricProofError("invalid target/prior boundary")
    closes = [row["close"] for row in records]
    result: dict[str, Any] = {
        "metric_semantics": "target_close_vs_provider_affine_prior_session_series",
        "target_trade_date": target_trade_date,
        "target_close": target_close,
        "prior_series_last_trade_date": records[-1]["trade_date"],
        "prior_record_count": len(records),
        "returns_pct": {},
        "moving_averages": {},
        "volume": {
            "status": "unavailable",
            "reason": "intraday reported_volume is not compatible with provider-affine daily session volume",
        },
        "relative_strength_pct": {"status": "unavailable"},
    }
    for sessions in (1, 3, 5):
        result["returns_pct"][f"{sessions}session"] = _pct(target_close, closes[-sessions]) if len(closes) >= sessions else None
    for window in (20, 50):
        key = f"ma{window}"
        if len(closes) >= window:
            ma = _mean(closes[-window:])
            result["moving_averages"][key] = ma
            result["moving_averages"][f"target_vs_{key}_pct"] = _pct(target_close, ma)
        else:
            result["moving_averages"][key] = None
            result["moving_averages"][f"target_vs_{key}_pct"] = None
    if benchmark_prior_records is not None or benchmark_target_close is not None:
        if benchmark_prior_records is None or benchmark_target_close is None:
            raise MetricProofError("benchmark inputs must be supplied together")
        benchmark = _validated_records(benchmark_prior_records)
        benchmark_target_close = _number(benchmark_target_close, "benchmark_target_close")
        subject_by_date = {row["trade_date"]: row["close"] for row in records}
        benchmark_by_date = {row["trade_date"]: row["close"] for row in benchmark}
        rs: dict[str, Any] = {"status": "available", "benchmark_target_close": benchmark_target_close}
        for sessions in (1, 3, 5):
            if len(records) < sessions:
                rs[f"{sessions}session"] = None
                continue
            baseline_date = records[-sessions]["trade_date"]
            benchmark_baseline = benchmark_by_date.get(baseline_date)
            if benchmark_baseline is None:
                rs[f"{sessions}session"] = None
                continue
            rs[f"{sessions}session"] = round(
                _pct(target_close, subject_by_date[baseline_date]) - _pct(benchmark_target_close, benchmark_baseline),
                6,
            )
        result["relative_strength_pct"] = rs
    return result


def _load_snapshot(root: Path, trade_date: str, stage: str) -> tuple[dict[str, Any], str, str]:
    latest_rel = f"snapshots/{trade_date[:7]}/{trade_date}/{stage}/latest.json"
    latest, _ = _read_json_exact(root, latest_rel)
    snapshot_rel = str(latest.get("snapshot_path") or "")
    snapshot_blob = str(latest.get("snapshot_blob_sha") or "")
    if not snapshot_rel or not HEX40.fullmatch(snapshot_blob):
        raise MetricProofError("snapshot latest pointer is incomplete")
    snapshot, actual = _read_json_exact(root, snapshot_rel, snapshot_blob)
    if snapshot.get("trade_date") != trade_date or snapshot.get("stage") != stage:
        raise MetricProofError("snapshot identity mismatch")
    return snapshot, snapshot_rel, actual


def _target_observations(root: Path, snapshot: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    targets: dict[str, dict[str, Any]] = {}
    refs = snapshot.get("data_refs") or []
    if not isinstance(refs, list):
        raise MetricProofError("snapshot data_refs must be a list")
    for ref in refs:
        if not isinstance(ref, Mapping):
            raise MetricProofError("snapshot data ref must be an object")
        rel = str(ref.get("path") or "")
        blob = str(ref.get("blob_sha") or "")
        if not rel or not HEX40.fullmatch(blob):
            raise MetricProofError("snapshot data ref is incomplete")
        capture, actual_blob = _read_json_exact(root, rel, blob)
        capture_session = str(capture.get("session") or "")
        if not capture_session:
            raise MetricProofError(f"snapshot capture session is missing: {rel}")
        for fact in capture.get("qualified_facts") or []:
            if not isinstance(fact, Mapping):
                continue
            symbol = str(fact.get("symbol") or "").upper()
            event_time = str(fact.get("event_time") or fact.get("source_timestamp") or "")
            price = fact.get("last_sale")
            fact_session = str(fact.get("session") or capture_session)
            if fact_session != capture_session:
                raise MetricProofError(f"capture/fact session mismatch: {rel}/{symbol}")
            if not symbol or not event_time or price is None:
                continue
            candidate = {
                "capture_path": rel,
                "capture_blob_sha": actual_blob,
                "event_time": event_time,
                "last_sale": _number(price, f"last_sale[{symbol}]"),
            }
            prior = targets.get(symbol)
            if prior is None or candidate["event_time"] > prior["event_time"]:
                targets[symbol] = candidate
                continue
            if candidate["event_time"] == prior["event_time"] and candidate != prior:
                raise MetricProofError(f"ambiguous selected target observation: {symbol}/{event_time}")
    return targets


def _target_matches_selected(target: Mapping[str, Any], selected: Mapping[str, Any], symbol: str) -> bool:
    for key in ("capture_path", "capture_blob_sha", "event_time"):
        if str(target.get(key) or "") != str(selected.get(key) or ""):
            return False
    return _number(target.get("last_sale"), f"proof target last_sale[{symbol}]") == _number(
        selected.get("last_sale"), f"selected target last_sale[{symbol}]"
    )


def verify_proof_source_blobs(root: Path, proof: Mapping[str, Any]) -> None:
    """Re-verify every proof source against one caller-pinned Store tree."""
    trade_date = str(proof.get("trade_date") or "")
    stage = str(proof.get("stage") or "")
    snapshot_ref = proof.get("snapshot") or {}
    snapshot_rel = str(snapshot_ref.get("path") or "")
    snapshot_blob = str(snapshot_ref.get("blob_sha") or "")
    if not snapshot_rel or not HEX40.fullmatch(snapshot_blob):
        raise MetricProofError("proof snapshot identity is incomplete")
    snapshot, _ = _read_json_exact(root, snapshot_rel, snapshot_blob)
    if snapshot.get("trade_date") != trade_date or snapshot.get("stage") != stage:
        raise MetricProofError("proof snapshot identity mismatch")
    if str(snapshot.get("snapshot_id") or "") != str(snapshot_ref.get("snapshot_id") or ""):
        raise MetricProofError("proof snapshot id mismatch")
    selected_targets = _target_observations(root, snapshot)

    for subject in proof.get("subjects") or []:
        if not isinstance(subject, Mapping):
            raise MetricProofError("proof subject must be an object")
        symbol = str(subject.get("symbol") or "").upper()
        target = subject.get("target") or {}
        if not isinstance(target, Mapping):
            raise MetricProofError(f"proof target must be an object: {symbol}")
        capture_rel = str(target.get("capture_path") or "")
        capture_blob = str(target.get("capture_blob_sha") or "")
        event_time = str(target.get("event_time") or "")
        if not symbol or not capture_rel or not HEX40.fullmatch(capture_blob) or not event_time:
            raise MetricProofError(f"proof target identity is incomplete: {symbol}")
        selected = selected_targets.get(symbol)
        if selected is None:
            raise MetricProofError(f"proof target symbol is absent from verified snapshot: {symbol}")
        if not _target_matches_selected(target, selected, symbol):
            raise MetricProofError(f"proof target is not the snapshot-selected observation: {symbol}")

        daily = subject.get("daily_series") or {}
        if not isinstance(daily, Mapping):
            raise MetricProofError(f"proof daily-series identity must be an object: {symbol}")
        index_rel = str(daily.get("index_path") or "")
        index_blob = str(daily.get("index_blob_sha") or "")
        if not index_rel or not HEX40.fullmatch(index_blob):
            raise MetricProofError(f"proof daily-series index identity is incomplete: {symbol}")
        index, _ = _read_json_exact(root, index_rel, index_blob)
        if index.get("provider") != PROVIDER or str(index.get("symbol") or "").upper() != symbol:
            raise MetricProofError(f"proof daily-series affinity mismatch: {symbol}")
        records = _canonical_daily_records(root, index_rel=index_rel, index=index, symbol=symbol)
        expected_shards = _exact_shard_refs(root, index_rel=index_rel, index=index, symbol=symbol)
        declared_shards = daily.get("shards") or []
        if not isinstance(declared_shards, list):
            raise MetricProofError(f"proof shard list is invalid: {symbol}")
        normalized_declared = [
            {"path": str(item.get("path") or ""), "blob_sha": str(item.get("blob_sha") or "")}
            for item in declared_shards
            if isinstance(item, Mapping)
        ]
        if normalized_declared != expected_shards:
            raise MetricProofError(f"proof shard identity set mismatch: {symbol}")
        prior = [row for row in records if str(row.get("trade_date")) < trade_date]
        if not prior:
            raise MetricProofError(f"proof daily series has no prior records: {symbol}")
        if int(daily.get("prior_record_count") or 0) != len(prior):
            raise MetricProofError(f"proof daily-series prior_record_count mismatch: {symbol}")
        if str(daily.get("prior_last_trade_date") or "") != str(prior[-1].get("trade_date") or ""):
            raise MetricProofError(f"proof daily-series prior_last_trade_date mismatch: {symbol}")


def build_proof(root: Path, *, trade_date: str, stage: str, data_plane_commit_sha: str) -> tuple[dict[str, Any], str]:
    if stage not in {"open_30m", "open_60m", "close"}:
        raise MetricProofError(f"unsupported metric-proof stage: {stage}")
    if not HEX40.fullmatch(data_plane_commit_sha):
        raise MetricProofError("data_plane_commit_sha must be a 40-char SHA")
    snapshot, snapshot_rel, snapshot_blob = _load_snapshot(root, trade_date, stage)
    targets = _target_observations(root, snapshot)
    daily_cache: dict[str, tuple[list[dict[str, Any]], dict[str, Any]]] = {}
    for symbol in sorted(set(targets) | {"SPY", "QQQ"}):
        try:
            daily_cache[symbol] = _load_daily_series(root, symbol, trade_date)
        except MetricProofUnavailable:
            continue

    subjects: list[dict[str, Any]] = []
    missing: list[dict[str, str]] = []
    for symbol in sorted(targets):
        if symbol not in daily_cache:
            missing.append({"symbol": symbol, "reason": "provider_affine_daily_series_unavailable"})
            continue
        prior, daily_meta = daily_cache[symbol]
        target = targets[symbol]
        base_metrics = compute_metrics(prior, target_trade_date=trade_date, target_close=target["last_sale"])
        benchmark_metrics: dict[str, Any] = {}
        for benchmark in ("SPY", "QQQ"):
            benchmark_target = targets.get(benchmark)
            benchmark_daily = daily_cache.get(benchmark)
            if benchmark_target is None or benchmark_daily is None:
                benchmark_metrics[benchmark] = None
                continue
            if benchmark_target["event_time"] != target["event_time"]:
                benchmark_metrics[benchmark] = None
                continue
            benchmark_prior, _ = benchmark_daily
            benchmark_metrics[benchmark] = compute_metrics(
                prior,
                target_trade_date=trade_date,
                target_close=target["last_sale"],
                benchmark_prior_records=benchmark_prior,
                benchmark_target_close=benchmark_target["last_sale"],
            )["relative_strength_pct"]
        subjects.append(
            {
                "symbol": symbol,
                "target": target,
                "daily_series": daily_meta,
                "metrics": base_metrics,
                "benchmark_metrics": benchmark_metrics,
            }
        )
    snapshot_generated_at = str(snapshot.get("generated_at") or "")
    if not snapshot_generated_at:
        raise MetricProofError("snapshot generated_at is required for deterministic proof identity")
    proof = {
        "schema_version": 1,
        "proof_kind": "stock_dairy_deterministic_metrics",
        "formula_contract": FORMULA_CONTRACT,
        "data_plane_commit_sha": data_plane_commit_sha,
        "trade_date": trade_date,
        "stage": stage,
        "snapshot": {"path": snapshot_rel, "blob_sha": snapshot_blob, "snapshot_id": str(snapshot["snapshot_id"])},
        "generated_at": snapshot_generated_at,
        "subjects": subjects,
        "missing": missing,
    }
    verify_proof_source_blobs(root, proof)
    rel = (
        f"proofs/deterministic-metrics/{trade_date[:7]}/{trade_date}/{stage}/"
        f"{data_plane_commit_sha}/{snapshot['snapshot_id']}.json"
    )
    return proof, rel


def _proof_pointer(proof: Mapping[str, Any], *, rel: str, blob: str) -> dict[str, Any]:
    snapshot = proof.get("snapshot") or {}
    return {
        "schema_version": 1,
        "proof_path": rel,
        "proof_blob_sha": blob,
        "snapshot_path": snapshot["path"],
        "snapshot_blob_sha": snapshot["blob_sha"],
        "snapshot_id": snapshot["snapshot_id"],
    }


def verify_existing_proof(
    root: Path,
    *,
    trade_date: str,
    stage: str,
    data_plane_commit_sha: str,
) -> tuple[str, str]:
    """Verify a persisted proof against the exact Store tree about to be published."""
    expected, rel = build_proof(
        root,
        trade_date=trade_date,
        stage=stage,
        data_plane_commit_sha=data_plane_commit_sha,
    )
    try:
        persisted, persisted_blob = _read_json_exact(root, rel)
    except FileNotFoundError as exc:
        raise MetricProofError(f"persisted proof missing from final Store tree: {rel}") from exc
    if canonical_bytes(persisted) != canonical_bytes(expected):
        raise MetricProofError("persisted proof does not match the final Store tree")
    verify_proof_source_blobs(root, persisted)
    latest_rel = f"proofs/deterministic-metrics/{trade_date[:7]}/{trade_date}/{stage}/latest.json"
    try:
        pointer, _ = _read_json_exact(root, latest_rel)
    except FileNotFoundError as exc:
        raise MetricProofError("persisted proof latest pointer missing from final Store tree") from exc
    if pointer != _proof_pointer(persisted, rel=rel, blob=persisted_blob):
        raise MetricProofError("persisted proof latest pointer does not match final Store tree")
    return rel, persisted_blob


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--store-root", required=True)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--stage", required=True, choices=["open_30m", "open_60m", "close"])
    parser.add_argument("--data-plane-commit-sha", required=True)
    parser.add_argument("--verify-existing", action="store_true")
    parser.add_argument("--print-path", action="store_true")
    args = parser.parse_args()
    root = Path(args.store_root)
    if args.verify_existing:
        rel, blob = verify_existing_proof(
            root,
            trade_date=args.trade_date,
            stage=args.stage,
            data_plane_commit_sha=args.data_plane_commit_sha,
        )
        if args.print_path:
            print(json.dumps({"proof_path": rel, "proof_blob_sha": blob, "status": "verified"}, sort_keys=True))
        return 0

    proof, rel = build_proof(
        root,
        trade_date=args.trade_date,
        stage=args.stage,
        data_plane_commit_sha=args.data_plane_commit_sha,
    )
    blob = _write_json(root / rel, proof)
    pointer = _proof_pointer(proof, rel=rel, blob=blob)
    latest = root / f"proofs/deterministic-metrics/{args.trade_date[:7]}/{args.trade_date}/{args.stage}/latest.json"
    latest.parent.mkdir(parents=True, exist_ok=True)
    latest.write_bytes(canonical_bytes(pointer) + b"\n")
    if args.print_path:
        print(json.dumps({"proof_path": rel, "proof_blob_sha": blob}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
