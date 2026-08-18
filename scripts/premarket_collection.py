#!/usr/bin/env python3
"""Qualified Nasdaq Premarket collection into the private Market Data Store.

Every invocation re-validates target trade date, 04:00-09:30 ET trade clocks,
source timestamps, requested security identity and delayed/reference semantics
before any fact reaches Store. Shadow probe/readiness artifacts are never read as
market facts by this collector.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

import collection_universe as collection
import market_data_collectors as base
from market_data_store import write_capture, write_snapshot
from nasdaq_extended_adapter import ExtendedHoursAdapterError, normalize_extended_trade_payload

ET = ZoneInfo("America/New_York")
PROVIDER = "nasdaq_public_extended"
SOURCE_CONTRACT = "nasdaq_public_extended_trade_detail_v1"
FEED_SCOPE = "official_website_delayed_extended_trade_detail"


def _identity(universe: Mapping[str, Any], symbol: str, asset_class: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "asset_class": asset_class,
        "ticker_effective_at": collection.ticker_effective_at(universe, symbol),
    }


def fetch_premarket(
    symbol: str,
    asset_class: str,
    trade_date: str,
    *,
    universe: Mapping[str, Any],
    access: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    spec = access["nasdaq_public_extended_premarket"]
    endpoint_id = str(spec["endpoint_id"])
    url = str(spec["url_template"]).format(symbol=symbol, asset_class=asset_class)
    payload = base._http_json(
        url,
        timeout=int(access["http"]["timeout_seconds"]),
        user_agent=str(access["http"]["user_agent"]),
    )
    normalized = normalize_extended_trade_payload(
        payload,
        session="premarket",
        symbol=symbol,
        asset_class=asset_class,
        endpoint_id=endpoint_id,
    )
    if normalized.get("trade_date") != trade_date:
        raise ExtendedHoursAdapterError(
            f"wrong Premarket trade date for {symbol}: {normalized.get('trade_date')} != {trade_date}"
        )

    identity = _identity(universe, symbol, asset_class)
    facts: list[dict[str, Any]] = []
    for candidate in normalized.get("qualified_candidate_facts") or []:
        if candidate.get("symbol") != symbol or candidate.get("asset_class") != asset_class:
            raise ExtendedHoursAdapterError(f"security identity mismatch for {symbol}")
        fact = dict(candidate)
        fact["provider"] = PROVIDER
        fact["source_contract"] = SOURCE_CONTRACT
        fact["feed_scope"] = FEED_SCOPE
        fact["security_identity"] = identity
        fact["realtime_claim"] = False
        facts.append(fact)

    if not facts:
        raise ExtendedHoursAdapterError(f"no qualified Premarket trade facts for {symbol}")
    facts.sort(key=lambda row: row["event_time"])
    return facts, {
        "provider": PROVIDER,
        "source_contract": SOURCE_CONTRACT,
        "url": url,
        "symbol": symbol,
        "asset_class": asset_class,
        "trade_date": trade_date,
        "last_update_timestamp_et": normalized.get("last_update_timestamp_et"),
        "actual_data_cutoff": normalized.get("actual_data_cutoff"),
        "qualified_trade_rows": len(facts),
        "security_identity": identity,
    }


def collect_premarket(
    *,
    trade_date: str,
    store_root: Path,
    universe_config: Mapping[str, Any],
    access: Mapping[str, Any],
    symbols_override: Sequence[str] | None = None,
) -> dict[str, Any]:
    full_universe = collection.premarket_universe(universe_config)
    by_symbol = dict(full_universe)
    if symbols_override is None:
        targets = full_universe
    else:
        requested = [str(symbol).upper() for symbol in symbols_override]
        unknown = sorted(set(requested) - set(by_symbol))
        if unknown:
            raise RuntimeError(f"Premarket symbols outside collection universe: {unknown}")
        targets = [(symbol, by_symbol[symbol]) for symbol in requested]

    generated_at = datetime.now(ET).isoformat()
    capture_refs: list[dict[str, Any]] = []
    missing: list[str] = []
    failures: dict[str, str] = {}
    diagnostics: dict[str, dict[str, Any]] = {}
    cutoffs: list[str] = []

    for symbol, asset_class in targets:
        try:
            facts, diag = fetch_premarket(
                symbol,
                asset_class,
                trade_date,
                universe=universe_config,
                access=access,
            )
            diagnostics[symbol] = diag
            cutoff = str(diag["actual_data_cutoff"])
            cutoffs.append(cutoff)
            rel, blob_sha = write_capture(
                store_root,
                trade_date=trade_date,
                session="premarket",
                provider=PROVIDER,
                capture_id=f"premarket-{symbol.lower()}-{datetime.now(ET).strftime('%H%M%S-et')}",
                generated_at=generated_at,
                actual_data_cutoff=cutoff,
                window={"start": "04:00", "end": "09:30"},
                feed_scope=FEED_SCOPE,
                qualified_facts=facts,
                missing_symbols=[],
            )
            capture_refs.append(
                {
                    "path": rel,
                    "blob_sha": blob_sha,
                    "kind": "premarket_trade_capture",
                    "window": "04:00-09:30",
                    "provider": PROVIDER,
                    "symbol": symbol,
                }
            )
        except Exception as exc:  # noqa: BLE001
            missing.append(symbol)
            failures[symbol] = f"{type(exc).__name__}:{exc}"

    if not capture_refs:
        return {
            "mode": "premarket",
            "status": "no_new_qualified_facts",
            "symbols_requested_in_increment": len(targets),
            "symbols_available_in_increment": 0,
            "symbols_missing_in_increment": len(missing),
            "missing": sorted(missing),
            "failures": failures,
            "diagnostics": diagnostics,
            "snapshot_written": False,
        }

    snapshot_id = f"premarket-{datetime.now(ET).strftime('%H%M%S-et')}"
    actual_cutoff = max(cutoffs)
    write_snapshot(
        store_root,
        stage="premarket",
        trade_date=trade_date,
        snapshot_id=snapshot_id,
        generated_at=generated_at,
        data_refs=capture_refs,
        coverage={
            "symbols_requested_in_increment": len(targets),
            "symbols_available_in_increment": len(capture_refs),
            "symbols_missing_in_increment": len(missing),
            "inherited_ref_count": 0,
            "total_ref_count": len(capture_refs),
            "provider_counts": {PROVIDER: len(capture_refs)},
        },
        missing=sorted(missing),
        target_window={"start": "04:00", "end": "09:30"},
        actual_data_cutoff=actual_cutoff,
    )
    return {
        "mode": "premarket",
        "status": "ok" if not missing else "partial",
        "symbols_requested_in_increment": len(targets),
        "symbols_available_in_increment": len(capture_refs),
        "symbols_missing_in_increment": len(missing),
        "missing": sorted(missing),
        "failures": failures,
        "diagnostics": diagnostics,
        "refs": len(capture_refs),
        "snapshot_written": True,
        "actual_data_cutoff": actual_cutoff,
        "source_contract": SOURCE_CONTRACT,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--store-root", default="sources/market-data")
    parser.add_argument("--universe", default="config/collection-universe.json")
    parser.add_argument("--access-config", default="config/market-data-collector-access.yaml")
    args = parser.parse_args()
    universe = collection.load_collection_universe(args.universe)
    access = base.load_yaml(args.access_config)
    result = collect_premarket(
        trade_date=args.trade_date,
        store_root=Path(args.store_root),
        universe_config=universe,
        access=access,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
