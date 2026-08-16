#!/usr/bin/env python3
"""First-class collection-universe authority for the independent Data Plane.

The collection universe is intentionally separate from stock-dairy research
watchlists.  This module validates collection membership and the explicit
semantics of cutoff-valid cross-asset proxy instruments before collectors run.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

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
    assets = asset_classes(universe)
    return sorted(assets.items())


def intraday_universe(universe: Mapping[str, Any]) -> list[tuple[str, str]]:
    assets = asset_classes(universe)
    symbols = [str(symbol).upper() for symbol in universe.get("intraday") or []]
    return sorted((symbol, assets[symbol]) for symbol in symbols)


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


def decorate_fact(universe: Mapping[str, Any], fact: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(fact)
    symbol = str(value.get("symbol") or "").upper()
    context = context_by_symbol(universe).get(symbol)
    if context:
        value["market_context"] = context
    return value


def validate_collection_universe(universe: Mapping[str, Any]) -> None:
    if int(universe.get("schema_version") or 0) != 3:
        raise RuntimeError("collection universe schema_version must be 3")

    entries = _daily_entries(universe)
    daily_symbols = [str(entry.get("symbol") or "").upper() for entry in entries]
    if not daily_symbols or any(not symbol for symbol in daily_symbols):
        raise RuntimeError("daily_series contains an empty symbol")
    if len(daily_symbols) != len(set(daily_symbols)):
        raise RuntimeError("daily_series symbols must be unique")

    intraday = [str(symbol).upper() for symbol in universe.get("intraday") or []]
    sectors = [str(symbol).upper() for symbol in universe.get("sector_etfs") or []]
    for group_name, symbols in (("intraday", intraday), ("sector_etfs", sectors)):
        if len(symbols) != len(set(symbols)):
            raise RuntimeError(f"{group_name} symbols must be unique")
        unknown = sorted(set(symbols) - set(daily_symbols))
        if unknown:
            raise RuntimeError(f"{group_name} symbols missing from daily_series: {unknown}")

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
            if symbol not in daily_symbols or symbol not in intraday:
                raise RuntimeError(f"context proxy must be collected in daily and intraday paths: {symbol}")


def compatibility_views(universe: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return in-memory legacy shapes for old collector primitives only.

    No file is written.  The authoritative membership remains collection-universe.json.
    This adapter can be deleted after every maintenance primitive accepts the universe
    object directly.
    """
    validate_collection_universe(universe)
    assets = asset_classes(universe)
    intraday = [symbol for symbol, _asset in intraday_universe(universe)]
    sectors = [str(symbol).upper() for symbol in universe.get("sector_etfs") or []]
    instruments: dict[str, dict[str, Any]] = {}
    for symbol, asset in assets.items():
        meta: dict[str, Any] = {"asset_class": asset}
        effective = ticker_effective_at(universe, symbol)
        if effective:
            meta["ticker_effective_at"] = effective
        instruments[symbol] = meta
    watchlist = {"core_watchlist": intraday, "instruments": instruments}
    completeness = {
        "minute_matrix_requirements": {"tracked_benchmarks": []},
        "sector_close_capability": {"exact_sector_etfs": sectors},
    }
    return watchlist, completeness
