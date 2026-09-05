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

HEX40 = re.compile(r"^[0-9a-f]{40}$")
PROVIDER = "twelve_data_basic"
FORMULA_CONTRACT = "stock_dairy_calculation_policy_v2"


class MetricProofError(RuntimeError):
    pass


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


def _load_daily_series(root: Path, symbol: str, target_trade_date: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    index_rel = f"series/daily/{PROVIDER}/{symbol.upper()}/_index.json"
    index, index_blob = _read_json_exact(root, index_rel)
    if index.get("provider") != PROVIDER or index.get("symbol") != symbol.upper():
        raise MetricProofError(f"series affinity mismatch: {symbol}")
    records: list[dict[str, Any]] = []
    shard_refs: list[dict[str, str]] = []
    series_dir = Path(index_rel).parent
    for meta in index.get("shards") or []:
        rel = str(series_dir / str(meta["path"]))
        shard, blob = _read_json_exact(root, rel, str(meta["blob_sha"]))
        if shard.get("series_id") != index.get("series_id"):
            raise MetricProofError(f"series id mismatch: {rel}")
        rows = shard.get("records") or []
        if not isinstance(rows, list):
            raise MetricProofError(f"invalid shard records: {rel}")
        records.extend(dict(row) for row in rows)
        shard_refs.append({"path": rel, "blob_sha": blob})
    dates = [str(row.get("trade_date")) for row in records]
    if dates != sorted(dates) or len(dates) != len(set(dates)):
        raise MetricProofError(f"series dates invalid: {symbol}")
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
    for ref in snapshot.get("data_refs") or []:
        rel = str(ref.get("path") or "")
        blob = str(ref.get("blob_sha") or "")
        if not rel or not HEX40.fullmatch(blob):
            raise MetricProofError("snapshot data ref is incomplete")
        capture, actual_blob = _read_json_exact(root, rel, blob)
        for fact in capture.get("qualified_facts") or []:
            if not isinstance(fact, dict):
                continue
            symbol = str(fact.get("symbol") or "").upper()
            event_time = str(fact.get("event_time") or fact.get("source_timestamp") or "")
            price = fact.get("last_sale")
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
    return targets


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
        except (FileNotFoundError, MetricProofError):
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
    rel = (
        f"proofs/deterministic-metrics/{trade_date[:7]}/{trade_date}/{stage}/"
        f"{data_plane_commit_sha}/{snapshot['snapshot_id']}.json"
    )
    return proof, rel


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--store-root", required=True)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--stage", required=True, choices=["open_30m", "open_60m", "close"])
    parser.add_argument("--data-plane-commit-sha", required=True)
    parser.add_argument("--print-path", action="store_true")
    args = parser.parse_args()
    root = Path(args.store_root)
    proof, rel = build_proof(
        root,
        trade_date=args.trade_date,
        stage=args.stage,
        data_plane_commit_sha=args.data_plane_commit_sha,
    )
    blob = _write_json(root / rel, proof)
    pointer = {
        "schema_version": 1,
        "proof_path": rel,
        "proof_blob_sha": blob,
        "snapshot_path": proof["snapshot"]["path"],
        "snapshot_blob_sha": proof["snapshot"]["blob_sha"],
        "snapshot_id": proof["snapshot"]["snapshot_id"],
    }
    latest = root / f"proofs/deterministic-metrics/{args.trade_date[:7]}/{args.trade_date}/{args.stage}/latest.json"
    latest.parent.mkdir(parents=True, exist_ok=True)
    latest.write_bytes(canonical_bytes(pointer) + b"\n")
    if args.print_path:
        print(json.dumps({"proof_path": rel, "proof_blob_sha": blob}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
