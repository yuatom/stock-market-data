#!/usr/bin/env python3
"""Evaluate Nasdaq extended-hours shadow probes against the promotion gate.

This script never promotes a provider and never creates market facts. It reads
non-authoritative probe diagnostics from the private Market Data Store and emits
a compact readiness record that can be reviewed before an explicit contract
change registers an extended-hours source.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import yaml

ET = ZoneInfo("America/New_York")
SESSIONS = ("premarket", "after_hours")


def _load_yaml(path: str | Path) -> dict[str, Any]:
    value = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected mapping: {path}")
    return value


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected object: {path}")
    return value


def _required_targets(spec: Mapping[str, Any]) -> dict[str, str]:
    declared: dict[str, str] = {}
    for raw in spec.get("probe_targets") or []:
        if not isinstance(raw, Mapping):
            raise ValueError("probe_targets entries must be mappings")
        symbol = str(raw.get("symbol") or "").strip().upper()
        asset_class = str(raw.get("asset_class") or "").strip().lower()
        if not symbol or asset_class not in {"stocks", "etf"}:
            raise ValueError(f"invalid probe target: {raw!r}")
        declared[symbol] = asset_class

    gate = spec.get("promotion_gate") or {}
    required = [
        str(symbol).strip().upper()
        for symbol in (
            list(gate.get("required_core_stock_targets") or [])
            + list(gate.get("required_market_anchor_targets") or [])
        )
    ]
    if not required:
        raise ValueError("promotion gate required targets must not be empty")
    missing = sorted(set(required) - set(declared))
    if missing:
        raise ValueError(f"promotion gate targets missing from probe_targets: {missing}")
    return {symbol: declared[symbol] for symbol in required}


def _shape_signature(result: Mapping[str, Any]) -> str | None:
    parts = []
    for field in (
        "data_top_level_keys",
        "info_table_first_row_keys",
        "trade_detail_first_row_keys",
    ):
        value = result.get(field)
        if not isinstance(value, list) or not value:
            return None
        parts.append(",".join(sorted(str(item) for item in value)))
    return "|".join(parts)


def _result_qualifies(
    result: Mapping[str, Any],
    *,
    session: str,
    symbol: str,
    asset_class: str,
) -> tuple[bool, list[str], str | None]:
    reasons: list[str] = []
    if str(result.get("session_candidate") or "") != session:
        reasons.append("wrong_session_candidate")
    if str(result.get("symbol") or "").upper() != symbol:
        reasons.append("wrong_symbol")
    if str(result.get("asset_class") or "").lower() != asset_class:
        reasons.append("wrong_asset_class")
    if result.get("http_status") != 200:
        reasons.append("http_not_200")
    if result.get("application_status_success") is not True:
        reasons.append("application_status_not_success")
    if result.get("error_class") not in (None, ""):
        reasons.append("error_class_present")
    if result.get("trade_date_resolvable_without_hindsight") is not True:
        reasons.append("trade_date_not_resolvable_without_hindsight")
    if result.get("trade_detail_all_rows_in_candidate_session_window") is not True:
        reasons.append("trade_rows_not_session_bounded")
    if int(result.get("trade_detail_time_parseable_count") or 0) <= 0:
        reasons.append("no_parseable_trade_times")
    if not result.get("trade_date_candidate"):
        reasons.append("trade_date_candidate_missing")
    if not result.get("last_update_timestamp_et"):
        reasons.append("last_update_timestamp_missing")
    lag = result.get("latest_trade_lag_seconds_vs_last_update")
    if not isinstance(lag, int) or lag < 0:
        reasons.append("delayed_semantics_not_preserved")
    shape = _shape_signature(result)
    if shape is None:
        reasons.append("response_shape_incomplete")
    return not reasons, reasons, shape


def _probe_files(store_root: Path) -> list[Path]:
    root = store_root / "collector-state" / "probes" / "nasdaq-extended"
    if not root.exists():
        return []
    return sorted(
        path
        for path in root.glob("**/*.json")
        if path.name != "promotion-readiness.json"
    )


def _evaluate_session(
    *,
    session: str,
    files: list[Path],
    required_targets: Mapping[str, str],
    minimum_sessions: int,
) -> dict[str, Any]:
    by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    malformed_files: list[str] = []

    for path in files:
        try:
            doc = _load_json(path)
        except Exception as exc:  # noqa: BLE001
            malformed_files.append(f"{path}:{type(exc).__name__}")
            continue
        if doc.get("probe") != "nasdaq_extended_hours":
            continue
        results = doc.get("results") or []
        if not isinstance(results, list):
            malformed_files.append(f"{path}:results_not_list")
            continue

        target_rows: dict[str, Mapping[str, Any]] = {}
        target_rejections: dict[str, list[str]] = {}
        shapes: dict[str, str] = {}
        trade_dates: set[str] = set()
        for symbol, asset_class in required_targets.items():
            candidates = [
                row
                for row in results
                if isinstance(row, Mapping)
                and str(row.get("session_candidate") or "") == session
                and str(row.get("symbol") or "").upper() == symbol
            ]
            accepted: Mapping[str, Any] | None = None
            rejected: list[str] = []
            accepted_shape: str | None = None
            for row in candidates:
                ok, reasons, shape = _result_qualifies(
                    row,
                    session=session,
                    symbol=symbol,
                    asset_class=asset_class,
                )
                if ok:
                    accepted = row
                    accepted_shape = shape
                    break
                rejected.extend(reasons)
            if accepted is not None:
                target_rows[symbol] = accepted
                if accepted_shape is not None:
                    shapes[symbol] = accepted_shape
                trade_dates.add(str(accepted.get("trade_date_candidate")))
            else:
                target_rejections[symbol] = sorted(set(rejected)) or ["target_not_present"]

        observed_date = None
        if len(trade_dates) == 1:
            observed_date = next(iter(trade_dates))
        generated_at = str(doc.get("generated_at") or "")
        record = {
            "probe_file": str(path),
            "generated_at": generated_at,
            "observed_trade_date": observed_date,
            "accepted_targets": sorted(target_rows),
            "missing_targets": sorted(set(required_targets) - set(target_rows)),
            "target_rejections": target_rejections,
            "shape_signatures": shapes,
            "full_target_coverage": len(target_rows) == len(required_targets),
            "single_trade_date": len(trade_dates) == 1,
        }
        date_key = observed_date or str(doc.get("probe_calendar_date_et") or "unknown")
        by_date[date_key].append(record)

    selected_by_date: dict[str, dict[str, Any]] = {}
    for date, observations in sorted(by_date.items()):
        observations.sort(key=lambda item: item.get("generated_at") or "")
        complete = [
            item
            for item in observations
            if item["full_target_coverage"] and item["single_trade_date"] and item["observed_trade_date"] == date
        ]
        selected = complete[-1] if complete else observations[-1]
        selected_by_date[date] = selected

    qualifying_dates = sorted(
        date
        for date, item in selected_by_date.items()
        if item["full_target_coverage"] and item["single_trade_date"] and item["observed_trade_date"] == date
    )

    target_shape_variants: dict[str, list[str]] = {}
    for symbol in required_targets:
        variants = sorted(
            {
                selected_by_date[date]["shape_signatures"].get(symbol)
                for date in qualifying_dates
                if selected_by_date[date]["shape_signatures"].get(symbol)
            }
        )
        target_shape_variants[symbol] = variants
    stable_shape = bool(qualifying_dates) and all(len(v) == 1 for v in target_shape_variants.values())
    enough_sessions = len(qualifying_dates) >= minimum_sessions
    promotion_ready = enough_sessions and stable_shape

    if promotion_ready:
        status = "promotion_ready"
    elif not enough_sessions:
        status = "insufficient_distinct_trade_sessions"
    else:
        status = "response_shape_not_stable"

    coverage_by_date = {
        date: {
            "probe_file": item["probe_file"],
            "generated_at": item["generated_at"],
            "full_target_coverage": item["full_target_coverage"],
            "single_trade_date": item["single_trade_date"],
            "accepted_target_count": len(item["accepted_targets"]),
            "required_target_count": len(required_targets),
            "missing_targets": item["missing_targets"],
            "target_rejections": item["target_rejections"],
        }
        for date, item in selected_by_date.items()
    }

    return {
        "session": session,
        "status": status,
        "promotion_ready": promotion_ready,
        "required_distinct_trade_sessions": minimum_sessions,
        "qualifying_trade_sessions": len(qualifying_dates),
        "qualifying_dates": qualifying_dates,
        "required_targets": sorted(required_targets),
        "stable_response_shape": stable_shape,
        "target_shape_variant_counts": {
            symbol: len(variants) for symbol, variants in sorted(target_shape_variants.items())
        },
        "coverage_by_date": coverage_by_date,
        "malformed_probe_files": malformed_files,
    }


def evaluate(
    *,
    config_path: str | Path,
    store_root: str | Path,
    sessions: list[str],
) -> dict[str, Any]:
    config = _load_yaml(config_path)
    spec = config.get("nasdaq_extended_hours_probe")
    if not isinstance(spec, Mapping):
        raise ValueError("nasdaq_extended_hours_probe config missing")
    gate = spec.get("promotion_gate") or {}
    minimum_sessions = int(gate.get("minimum_distinct_trade_sessions") or 0)
    if minimum_sessions <= 0:
        raise ValueError("minimum_distinct_trade_sessions must be positive")
    required_targets = _required_targets(spec)
    files = _probe_files(Path(store_root))
    session_results = {
        session: _evaluate_session(
            session=session,
            files=files,
            required_targets=required_targets,
            minimum_sessions=minimum_sessions,
        )
        for session in sessions
    }
    return {
        "schema_version": 1,
        "generated_at": datetime.now(ET).isoformat(),
        "market_fact_authority": False,
        "automatic_promotion_allowed": False,
        "purpose": "nasdaq_extended_hours_explicit_promotion_readiness",
        "probe_file_count": len(files),
        "sessions": session_results,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/market-data-collector-access.yaml")
    parser.add_argument("--store-root", default="sources/market-data")
    parser.add_argument("--session", choices=["all", *SESSIONS], default="all")
    parser.add_argument("--output")
    args = parser.parse_args()

    sessions = list(SESSIONS) if args.session == "all" else [args.session]
    result = evaluate(config_path=args.config, store_root=args.store_root, sessions=sessions)
    text = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
