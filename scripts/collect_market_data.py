#!/usr/bin/env python3
"""Stable entrypoint for the bounded market-data collector.

Open30/Open60 are independently recoverable: if a required predecessor Stage
Snapshot is absent or partial, the entrypoint materializes that exact prior
window first, then runs the requested increment. This prevents the live
on-demand handoff from depending on an earlier scheduled Task having succeeded.

Daily Series writes also pass the provider-settlement maturity policy owned by
``config/market-data-store.yaml`` before immutable append-only storage is
allowed.  This keeps provisional post-close OHLCV values out of the reusable
history without moving any user-facing report schedule.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import market_data_collectors as base
from market_data_collection import main

ET = ZoneInfo("America/New_York")


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


def _snapshot_complete(store_root: Path, trade_date: str, stage: str) -> bool:
    refs, missing = base._load_prior_snapshot(store_root, trade_date, stage)
    return bool(refs) and not missing


def daily_series_settlement_mature(
    *,
    trade_date: str,
    config: Mapping[str, Any],
    now_et: datetime,
) -> tuple[bool, str | None]:
    """Return whether immutable Daily Series writes may run for this ET date.

    A past trade-date maintenance run is already beyond the live maturity gate.
    A future date is invalid.  For the current ET date the owner contract gives
    the earliest local time at which the provider's previous-session daily bar
    is eligible for immutable append.
    """
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
    mature, reason = daily_series_settlement_mature(
        trade_date=trade_date,
        config=config,
        now_et=now,
    )
    if mature:
        return None
    if reason == "trade_date_is_in_the_future":
        raise SystemExit(f"previous_session_eod trade_date {trade_date} is in the future relative to ET")
    return {
        "mode": "previous_session_eod",
        "status": "settlement_not_mature",
        "trade_date": trade_date,
        "observed_at_et": now.isoformat(),
        "reason": reason,
        "changed_series": 0,
        "failures": [],
    }


def _ensure_predecessors(argv: list[str]) -> None:
    mode = str(_arg_value(argv, "--mode") or "")
    trade_date = _arg_value(argv, "--trade-date")
    if not trade_date:
        # The underlying collector owns current-ET date resolution. On-demand
        # requests always pass --trade-date; scheduled collectors can remain
        # independent and do not need dependency recovery before their window.
        return
    store_root = Path(str(_arg_value(argv, "--store-root", "sources/market-data")))

    required: list[str]
    if mode == "open_30m":
        required = ["open_15m"]
    elif mode == "open_60m":
        required = ["open_15m", "open_30m"]
    else:
        return

    for stage in required:
        if _snapshot_complete(store_root, trade_date, stage):
            continue
        print(f"dependency_snapshot_missing_or_partial stage={stage}; materializing exact predecessor window")
        rc = main(_replace_mode(argv, stage))
        if rc != 0:
            raise SystemExit(rc)
        if not _snapshot_complete(store_root, trade_date, stage):
            # Do not fabricate completeness. The requested later stage still
            # runs and will carry predecessor missing symbols into its snapshot.
            print(f"dependency_snapshot_still_partial stage={stage}; continuing claim-scoped")


def cli(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    gate = _settlement_gate_result(args)
    if gate is not None:
        print(json.dumps(gate, ensure_ascii=False, sort_keys=True))
        return 0
    _ensure_predecessors(args)
    return main(args)


if __name__ == "__main__":
    raise SystemExit(cli())
