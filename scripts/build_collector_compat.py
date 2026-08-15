#!/usr/bin/env python3
"""Build ephemeral compatibility inputs for the migrated collector runtime.

The standalone Data Plane owns collection membership in
``config/collection-universe.json``.  The imported collector implementation
still accepts the former watchlist/data-completeness-shaped arguments, so this
helper deterministically projects the collection universe into temporary files.
The generated files are runtime adapters only and are never semantic owners or
persisted Market Data Store assets.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("collection universe must be an object")
    return value


def _validate(universe: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    if universe.get("schema_version") != 2:
        raise ValueError("collection universe schema_version must be 2")
    daily = universe.get("daily_series") or []
    intraday = [str(x).upper() for x in (universe.get("intraday") or [])]
    sectors = [str(x).upper() for x in (universe.get("sector_etfs") or [])]
    if not isinstance(daily, list) or not daily:
        raise ValueError("daily_series must be non-empty")
    normalized: list[dict[str, Any]] = []
    symbols: set[str] = set()
    for raw in daily:
        if not isinstance(raw, Mapping):
            raise ValueError("daily_series entries must be objects")
        symbol = str(raw.get("symbol") or "").upper()
        asset_class = str(raw.get("asset_class") or "")
        if not symbol or asset_class not in {"stocks", "etf"}:
            raise ValueError(f"invalid daily-series entry: {raw!r}")
        if symbol in symbols:
            raise ValueError(f"duplicate daily-series symbol: {symbol}")
        symbols.add(symbol)
        item: dict[str, Any] = {"symbol": symbol, "asset_class": asset_class}
        if raw.get("ticker_effective_at"):
            item["ticker_effective_at"] = str(raw["ticker_effective_at"])
        normalized.append(item)
    if len(intraday) != len(set(intraday)) or not set(intraday).issubset(symbols):
        raise ValueError("intraday must be unique and a subset of daily_series")
    if len(sectors) != len(set(sectors)) or not set(sectors).issubset(symbols):
        raise ValueError("sector_etfs must be unique and a subset of daily_series")
    return normalized, intraday, sectors


def build(universe: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    daily, intraday, sectors = _validate(universe)
    instruments = {
        row["symbol"]: {
            "asset_class": row["asset_class"],
            **({"ticker_effective_at": row["ticker_effective_at"]} if row.get("ticker_effective_at") else {}),
        }
        for row in daily
    }
    watchlist_compat = {
        "version": 1,
        "generated_role": "ephemeral_collector_compat_only",
        "market_fact_authority": False,
        "research_authority": False,
        "generated_from": "config/collection-universe.json",
        "core_watchlist": intraday,
        "instruments": instruments,
    }
    completeness_compat = {
        "schema_version": 1,
        "generated_role": "ephemeral_collector_compat_only",
        "market_fact_authority": False,
        "research_usability_authority": False,
        "generated_from": "config/collection-universe.json",
        "minute_matrix_requirements": {"tracked_benchmarks": []},
        "sector_close_capability": {"exact_sector_etfs": sectors},
    }
    return watchlist_compat, completeness_compat


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--universe", default="config/collection-universe.json")
    parser.add_argument("--output-dir", default=".runtime")
    args = parser.parse_args(argv)
    universe = _load(Path(args.universe))
    watchlist, completeness = build(universe)
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    (output / "collector-watchlist.json").write_text(
        json.dumps(watchlist, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    (output / "collector-completeness.yaml").write_text(
        yaml.safe_dump(completeness, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    print(json.dumps({
        "status": "ok",
        "daily_series_count": len(watchlist["instruments"]),
        "intraday_count": len(watchlist["core_watchlist"]),
        "sector_etf_count": len(completeness["sector_close_capability"]["exact_sector_etfs"]),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
