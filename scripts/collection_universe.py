#!/usr/bin/env python3
"""First-class collection-universe authority for the independent Data Plane.

The collection universe is intentionally separate from stock-dairy research
watchlists. This module validates collection membership and explicit semantics
before collectors run.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

CONTEXT_PROXY_ROLE = "cutoff_valid_cross_asset_proxy"
REQUIRED_CONTEXT_CATEGORIES = ("rates", "volatility", "dollar", "commodities", "crypto")


def load_collection_universe(path: str | Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("collection universe must be a JSON object")
    validate_collection_universe(value)
    return value


def _daily_entries(universe: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    entries = universe.get("daily_series") or []
    if not isinstance(entries, list):
        raise RuntimeError("daily_series must be an array")
    return [entry for entry in entries if isinstance(entry, Mapping)]


def asset_classes(universe: Mapping[str, Any]) -> dict[str, str]:
    return {str(entry["symbol"]).upper(): str(entry["asset_class"]) for entry in _daily_entries(universe)}


def daily_universe(universe: Mapping[str, Any]) -> list[tuple[str, str]]:
    return sorted(asset_classes(universe).items())


def premarket_universe(universe: Mapping[str, Any]) -> list[tuple[str, str]]:
    assets = asset_classes(universe)
    symbols = [str(symbol).upper() for symbol in universe.get("premarket") or []]
    return sorted((symbol, assets[symbol]) for symbol in symbols)


def intraday_universe(universe: Mapping[str, Any]) -> list[tuple[str, str]]:
    assets = asset_classes(universe)
    symbols = [str(symbol).upper() for symbol in universe.get("intraday") or []]
    return sorted((symbol, assets[symbol]) for symbol in symbols)


def sector_symbols(universe: Mapping[str, Any]) -> list[str]:
    return sorted(str(symbol).upper() for symbol in universe.get("sector_etfs") or [])


def sector_universe(universe: Mapping[str, Any]) -> list[tuple[str, str]]:
    assets = asset_classes(universe)
    return [(symbol, assets[symbol]) for symbol in sector_symbols(universe)]


def ticker_effective_at(universe: Mapping[str, Any], symbol: str) -> str | None:
    target = symbol.upper()
    for entry in _daily_entries(universe):
        if str(entry.get("symbol") or "").upper() == target:
            value = entry.get("ticker_effective_at")
            return str(value) if value else None
    return None


def context_proxies(universe: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    raw = universe.get("context_proxies") or {}
    if not isinstance(raw, Mapping):
        raise RuntimeError("context_proxies must be an object")
    return {str(category): dict(spec) for category, spec in raw.items() if isinstance(spec, Mapping)}


def context_by_symbol(universe: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for category, spec in context_proxies(universe).items():
        for symbol in spec.get("symbols") or []:
            symbol = str(symbol).upper()
            if symbol in result:
                raise RuntimeError(f"context proxy symbol belongs to multiple categories: {symbol}")
            result[symbol] = {
                "category": category,
                "quality_role": str(spec["quality_role"]),
                "semantics": str(spec["semantics"]),
                "not_equivalent_to": [str(value) for value in spec.get("not_equivalent_to") or []],
            }
    return result


def context_symbols(universe: Mapping[str, Any]) -> list[str]:
    return sorted(context_by_symbol(universe))


def close_supported_baseline_symbols(universe: Mapping[str, Any]) -> list[str]:
    return sorted(set(context_symbols(universe)) | set(sector_symbols(universe)))


def close_supported_baseline_universe(universe: Mapping[str, Any]) -> list[tuple[str, str]]:
    assets = asset_classes(universe)
    return [(symbol, assets[symbol]) for symbol in close_supported_baseline_symbols(universe)]


def decorate_fact(universe: Mapping[str, Any], fact: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(fact)
    symbol = str(value.get("symbol") or "").upper()
    context = context_by_symbol(universe).get(symbol)
    if context:
        value["market_context"] = context
    return value


def _validated_group(universe: Mapping[str, Any], name: str, daily_symbols: set[str]) -> list[str]:
    raw = universe.get(name) or []
    if not isinstance(raw, list):
        raise RuntimeError(f"{name} must be an array")
    symbols = [str(symbol).upper() for symbol in raw]
    if not symbols or any(not symbol for symbol in symbols):
        raise RuntimeError(f"{name} must contain non-empty symbols")
    if len(symbols) != len(set(symbols)):
        raise RuntimeError(f"{name} symbols must be unique")
    unknown = sorted(set(symbols) - daily_symbols)
    if unknown:
        raise RuntimeError(f"{name} symbols missing from daily_series: {unknown}")
    return symbols


def validate_collection_universe(universe: Mapping[str, Any]) -> None:
    if int(universe.get("schema_version") or 0) != 4:
        raise RuntimeError("collection universe schema_version must be 4")

    entries = _daily_entries(universe)
    daily = [str(entry.get("symbol") or "").upper() for entry in entries]
    if not daily or any(not symbol for symbol in daily):
        raise RuntimeError("daily_series contains an empty symbol")
    if len(daily) != len(set(daily)):
        raise RuntimeError("daily_series symbols must be unique")
    daily_set = set(daily)

    premarket = _validated_group(universe, "premarket", daily_set)
    intraday = _validated_group(universe, "intraday", daily_set)
    sectors = _validated_group(universe, "sector_etfs", daily_set)
    if not set(premarket).issubset(intraday):
        raise RuntimeError("premarket symbols must be a subset of intraday collection membership")
    expected_premarket = {"TME","PLTR","SPCX","NVDA","TSLA","MU","META","ORCL","TSM","AMD","QQQ","SPY"}
    if set(premarket) != expected_premarket:
        raise RuntimeError("premarket collection group must contain exactly 10 Core plus QQQ/SPY")

    proxies = context_proxies(universe)
    if tuple(sorted(proxies)) != tuple(sorted(REQUIRED_CONTEXT_CATEGORIES)):
        raise RuntimeError(
            "context_proxies must define exactly the supported baseline categories: "
            + ",".join(REQUIRED_CONTEXT_CATEGORIES)
        )
    seen: set[str] = set()
    for category in REQUIRED_CONTEXT_CATEGORIES:
        spec = proxies[category]
        if str(spec.get("quality_role") or "") != CONTEXT_PROXY_ROLE:
            raise RuntimeError(f"{category} quality_role must be {CONTEXT_PROXY_ROLE}")
        if not str(spec.get("semantics") or "").strip():
            raise RuntimeError(f"{category} semantics must be explicit")
        not_equivalent = [str(value).strip() for value in spec.get("not_equivalent_to") or []]
        if not not_equivalent or any(not value for value in not_equivalent):
            raise RuntimeError(f"{category} must declare not_equivalent_to guards")
        symbols = [str(symbol).upper() for symbol in spec.get("symbols") or []]
        if not symbols:
            raise RuntimeError(f"{category} must contain at least one proxy symbol")
        for symbol in symbols:
            if symbol in seen:
                raise RuntimeError(f"context proxy symbol belongs to multiple categories: {symbol}")
            seen.add(symbol)
            if symbol not in daily_set or symbol not in intraday:
                raise RuntimeError(f"context proxy must be collected in daily and intraday paths: {symbol}")

    supported = close_supported_baseline_symbols(universe)
    expected_count = len(set(sectors)) + len(seen - set(sectors))
    if len(supported) != expected_count:
        raise RuntimeError("Close supported baseline symbol derivation contains duplicate or missing membership")


def compatibility_views(universe: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return in-memory legacy shapes for maintenance primitives only."""
    validate_collection_universe(universe)
    assets = asset_classes(universe)
    intraday = [symbol for symbol, _asset in intraday_universe(universe)]
    sectors = sector_symbols(universe)
    instruments: dict[str, dict[str, Any]] = {}
    for symbol, asset in assets.items():
        meta: dict[str, Any] = {"asset_class": asset}
        effective = ticker_effective_at(universe, symbol)
        if effective:
            meta["ticker_effective_at"] = effective
        instruments[symbol] = meta
    return (
        {"core_watchlist": intraday, "instruments": instruments},
        {"minute_matrix_requirements": {"tracked_benchmarks": []}, "sector_close_capability": {"exact_sector_etfs": sectors}},
    )
