#!/usr/bin/env python3
"""Stable entrypoint for the bounded market-data collector.

Open30/Open60 are independently recoverable: if a required predecessor Stage
Snapshot is absent, the entrypoint materializes that exact prior window first,
then runs the requested increment. A materialized partial predecessor is valid
claim-scoped inherited context and must not be refetched merely to chase full
coverage; doing so can starve the requested later-stage window when provider
coverage is intentionally limited.

Daily Series writes also pass the provider-settlement maturity policy owned by
``config/market-data-store.yaml`` before immutable append-only storage is
allowed.  This keeps provisional post-close OHLCV values out of the reusable
history without moving any user-facing report schedule.

Live Close additionally collects the dedicated 11-sector collection group in
the 15:45-16:00 window without expanding Open15/Open30/Open60 membership. This
keeps the exact Close sector surface available without spending sector credits
at every intraday stage.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import build_deterministic_metric_proof as metric_proof
import collection_universe as collection
import market_data_collectors as base
import market_data_collection as runtime

ET = ZoneInfo("America/New_York")

REGULAR_STAGE_CUTOFF_ET: dict[str, tuple[int, int]] = {
    "open_15m": (9, 45),
    "open_30m": (10, 0),
    "open_60m": (10, 30),
}
REGULAR_STAGE_PREWARM_MAX_SECONDS = 6 * 60


def _arg_value(argv: list[str], flag: str, default: str | None = None) -> str | None:
    try:
        index = argv.index(flag)
    except ValueError:
        return default
    if index + 1 >= len(argv):
        raise SystemExit(f"{flag} requires a value")
    return argv[index + 1]


def _replace_mode(argv: list[str], mode: str) -> list[str]:
    out = list(argv)
    try:
        index = out.index("--mode")
    except ValueError as exc:
        raise SystemExit("--mode is required") from exc
    out[index + 1] = mode
    return out


def _snapshot_materialized(store_root: Path, trade_date: str, stage: str) -> bool:
    refs, _missing = base._load_prior_snapshot(store_root, trade_date, stage)
    return bool(refs)


def regular_stage_cutoff_wait_seconds(*, mode: str, trade_date: str, now_et: datetime) -> float:
    cutoff = REGULAR_STAGE_CUTOFF_ET.get(mode)
    if cutoff is None:
        return 0.0
    if now_et.tzinfo is None:
        now_et = now_et.replace(tzinfo=ET)
    else:
        now_et = now_et.astimezone(ET)
    current_date = now_et.date().isoformat()
    if trade_date > current_date:
        raise SystemExit(f"{mode} trade_date {trade_date} is in the future relative to ET")
    if trade_date < current_date:
        return 0.0
    cutoff_at = now_et.replace(hour=cutoff[0], minute=cutoff[1], second=0, microsecond=0)
    wait_seconds = (cutoff_at - now_et).total_seconds()
    if wait_seconds <= 0:
        return 0.0
    if wait_seconds > REGULAR_STAGE_PREWARM_MAX_SECONDS:
        raise SystemExit(f"{mode} provider access is too early: {wait_seconds:.0f}s before semantic cutoff")
    return wait_seconds


def _wait_until_regular_stage_cutoff(argv: list[str], *, now_et: datetime | None = None) -> None:
    mode = str(_arg_value(argv, "--mode") or "")
    if mode not in REGULAR_STAGE_CUTOFF_ET:
        return
    now = (now_et or datetime.now(ET)).astimezone(ET)
    trade_date = str(_arg_value(argv, "--trade-date") or now.date().isoformat())
    wait_seconds = regular_stage_cutoff_wait_seconds(mode=mode, trade_date=trade_date, now_et=now)
    if wait_seconds <= 0:
        return
    print(json.dumps({"mode": mode, "status": "prewarm_waiting_for_semantic_cutoff", "trade_date": trade_date, "observed_at_et": now.isoformat(), "wait_seconds": round(wait_seconds, 3)}, sort_keys=True))
    time.sleep(wait_seconds)


def daily_series_settlement_mature(*, trade_date: str, config: Mapping[str, Any], now_et: datetime) -> tuple[bool, str | None]:
    if now_et.tzinfo is None:
        now_et = now_et.replace(tzinfo=ET)
    else:
        now_et = now_et.astimezone(ET)
    current_date = now_et.date().isoformat()
    if trade_date > current_date:
        return False, "trade_date_is_in_the_future"
    if trade_date < current_date:
        return True, None
    daily = ((config.get("collector") or {}).get("twelve_data_daily") or {})
    maturity = daily.get("settlement_maturity") or {}
    minimum_et = str(maturity.get("minimum_next_day_et") or "")
    if len(minimum_et) != 5 or minimum_et[2] != ":":
        return False, "settlement_maturity_contract_invalid"
    if now_et.strftime("%H:%M") < minimum_et:
        return False, f"provider_settlement_not_mature_before_{minimum_et}_ET"
    return True, None


def _settlement_gate_result(argv: list[str], *, now_et: datetime | None = None) -> dict[str, Any] | None:
    mode = str(_arg_value(argv, "--mode") or "")
    if mode != "previous_session_eod":
        return None
    config_path = Path(str(_arg_value(argv, "--config", "config/market-data-store.yaml")))
    config = base.load_yaml(config_path)
    now = (now_et or datetime.now(ET)).astimezone(ET)
    trade_date = str(_arg_value(argv, "--trade-date") or now.date().isoformat())
    mature, reason = daily_series_settlement_mature(trade_date=trade_date, config=config, now_et=now)
    if mature:
        return None
    if reason == "trade_date_is_in_the_future":
        raise SystemExit(f"previous_session_eod trade_date {trade_date} is in the future relative to ET")
    return {"mode": "previous_session_eod", "status": "settlement_not_mature", "trade_date": trade_date, "observed_at_et": now.isoformat(), "reason": reason, "changed_series": 0, "failures": []}


def _ensure_predecessors(argv: list[str]) -> None:
    mode = str(_arg_value(argv, "--mode") or "")
    trade_date = _arg_value(argv, "--trade-date")
    if not trade_date:
        return
    store_root = Path(str(_arg_value(argv, "--store-root", "sources/market-data")))
    if mode == "open_30m":
        required = ["open_15m"]
    elif mode == "open_60m":
        required = ["open_15m", "open_30m"]
    else:
        return
    for stage in required:
        if _snapshot_materialized(store_root, trade_date, stage):
            continue
        print(f"dependency_snapshot_absent stage={stage}; materializing exact predecessor window")
        rc = runtime.main(_replace_mode(argv, stage))
        if rc != 0:
            raise SystemExit(rc)
        if not _snapshot_materialized(store_root, trade_date, stage):
            print(f"dependency_snapshot_still_absent stage={stage}; continuing claim-scoped")


def _live_close_universe(universe_config: Mapping[str, Any]) -> list[tuple[str, str]]:
    assets = collection.asset_classes(universe_config)
    symbols = {symbol for symbol, _asset in collection.intraday_universe(universe_config)} | set(collection.sector_symbols(universe_config))
    return [(symbol, assets[symbol]) for symbol in sorted(symbols)]


def _run_live_close(argv: list[str]) -> int | None:
    mode = str(_arg_value(argv, "--mode") or "")
    if mode not in {"close", "close_retry", "close_final"}:
        return None
    trade_date = str(_arg_value(argv, "--trade-date") or datetime.now(ET).date().isoformat())
    store_root = Path(str(_arg_value(argv, "--store-root", "sources/market-data")))
    universe_path = str(_arg_value(argv, "--universe", "config/collection-universe.json"))
    config_path = str(_arg_value(argv, "--config", "config/market-data-store.yaml"))
    access_path = str(_arg_value(argv, "--access-config", "config/market-data-collector-access.yaml"))
    universe_config = collection.load_collection_universe(universe_path)
    config = base.load_yaml(config_path)
    access = base.load_yaml(access_path)
    eligible = _live_close_universe(universe_config)
    symbols_override = None
    if mode in {"close_retry", "close_final"}:
        symbols_override = runtime._retry_symbols(store_root, trade_date, universe_config)
        if not symbols_override:
            print(json.dumps({"mode": mode, "status": "nothing_missing", "changed": 0}, sort_keys=True))
            return 0
    result = runtime.collect_regular_window(mode=mode, stage="close", trade_date=trade_date, start_et="15:45", end_et="16:00", store_root=store_root, universe_config=universe_config, config=config, access=access, symbols_override=symbols_override, eligible_universe=eligible)
    result["terminal_semantics"] = "timestamped_regular_session_context_provider_specific_not_official_close_or_sip"
    result["live_close_sector_surface"] = {"required_sector_symbols": collection.sector_symbols(universe_config), "sector_symbols_requested": result.get("sector_symbols_requested", [])}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


def _build_metric_proof_if_applicable(argv: list[str]) -> None:
    mode = str(_arg_value(argv, "--mode") or "")
    stage = "close" if mode in {"close", "close_retry", "close_final"} else mode
    if stage not in {"open_30m", "open_60m", "close"}:
        return
    trade_date = str(_arg_value(argv, "--trade-date") or datetime.now(ET).date().isoformat())
    store_root = Path(str(_arg_value(argv, "--store-root", "sources/market-data")))
    compute_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    proof, rel = metric_proof.build_proof(store_root, trade_date=trade_date, stage=stage, data_plane_commit_sha=compute_sha)
    blob = metric_proof._write_json(store_root / rel, proof)
    pointer = {"schema_version": 1, "proof_path": rel, "proof_blob_sha": blob, "snapshot_path": proof["snapshot"]["path"], "snapshot_blob_sha": proof["snapshot"]["blob_sha"], "snapshot_id": proof["snapshot"]["snapshot_id"]}
    latest = store_root / f"proofs/deterministic-metrics/{trade_date[:7]}/{trade_date}/{stage}/latest.json"
    latest.parent.mkdir(parents=True, exist_ok=True)
    latest.write_bytes(metric_proof.canonical_bytes(pointer) + b"\n")
    print(json.dumps({"metric_proof_status": "written", "metric_proof_path": rel, "metric_proof_blob_sha": blob}, sort_keys=True))


def cli(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    gate = _settlement_gate_result(args)
    if gate is not None:
        print(json.dumps(gate, ensure_ascii=False, sort_keys=True))
        return 0
    _wait_until_regular_stage_cutoff(args)
    _ensure_predecessors(args)
    close_result = _run_live_close(args)
    if close_result is not None:
        if close_result == 0:
            _build_metric_proof_if_applicable(args)
        return close_result
    rc = runtime.main(args)
    if rc == 0:
        _build_metric_proof_if_applicable(args)
    return rc


if __name__ == "__main__":
    raise SystemExit(cli())
