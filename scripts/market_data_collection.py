#!/usr/bin/env python3
"""Canonical market-data collection orchestration.

Provider primitives remain in market_data_collectors.py. This module owns the
regular-session collection path, cutoff-valid cross-asset proxy persistence,
and the narrowly authorized historical context repair path. Collection
membership comes only from config/collection-universe.json; no research
watchlist compatibility file is authoritative here.
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

import collection_universe as collection
import market_data_collectors as base
from market_data_store import write_capture, write_snapshot

ET = ZoneInfo("America/New_York")
NASDAQ = "nasdaq_public_intraday"
TWELVE = "twelve_data_basic"
HISTORICAL_CONTEXT_REPAIR = "historical_context_repair"


def _number(value: Any) -> float | None:
    if value in (None, "", "N/A"):
        return None
    text = str(value).replace("$", "").replace(",", "").strip()
    try:
        return float(text)
    except ValueError:
        return None


def _window_bounds(trade_date: str, start_et: str, end_et: str) -> tuple[datetime, datetime]:
    start = datetime.fromisoformat(f"{trade_date}T{start_et}:00").replace(tzinfo=ET)
    end = datetime.fromisoformat(f"{trade_date}T{end_et}:00").replace(tzinfo=ET)
    return start, end


def _parse_nasdaq_timestamp(value: Any) -> tuple[datetime | None, str | None]:
    try:
        raw = int(value)
    except (TypeError, ValueError):
        return None, None
    if raw > 10_000_000_000:
        seconds = raw / 1000.0
        unit = "milliseconds"
    else:
        seconds = float(raw)
        unit = "seconds"
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).astimezone(ET), unit
    except (OverflowError, OSError, ValueError):
        return None, unit


def fetch_nasdaq_regular(
    symbol: str,
    asset_class: str,
    trade_date: str,
    start_et: str,
    end_et: str,
    access: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    spec = access["nasdaq_public_intraday"]
    url = str(spec["regular_session_endpoint_template"]).format(
        symbol=symbol,
        asset_class=asset_class,
    )
    payload = base._http_json(
        url,
        timeout=int(access["http"]["timeout_seconds"]),
        user_agent=str(access["http"]["user_agent"]),
    )
    chart: Any = payload
    for token in str(spec.get("chart_array") or "data.chart").split("."):
        chart = chart.get(token) if isinstance(chart, dict) else None
    rows = chart if isinstance(chart, list) else []
    start, end = _window_bounds(trade_date, start_et, end_et)
    facts: list[dict[str, Any]] = []
    parseable = 0
    date_match = 0
    units: set[str] = set()
    sample_raw = None
    ts_field = str(spec.get("timestamp_field") or spec.get("timestamp_ms_field") or "x")
    price_field = str(spec.get("last_sale_price_field") or "y")
    volume_field = str(spec.get("reported_minute_volume_field") or "w")
    for row in rows:
        if not isinstance(row, dict):
            continue
        if sample_raw is None:
            sample_raw = row.get(ts_field)
        ts, unit = _parse_nasdaq_timestamp(row.get(ts_field))
        if unit:
            units.add(unit)
        if ts is None:
            continue
        parseable += 1
        if ts.date().isoformat() != trade_date:
            continue
        date_match += 1
        if not (start <= ts < end):
            continue
        price = _number(row.get(price_field))
        volume = _number(row.get(volume_field))
        if price is None:
            continue
        facts.append(
            {
                "symbol": symbol,
                "asset_class": asset_class,
                "session": "regular",
                "event_time": ts.isoformat(),
                "source_timestamp": ts.isoformat(),
                "last_sale": price,
                "reported_volume": max(volume or 0.0, 0.0),
                "source_contract": str(spec.get("source_contract") or "nasdaq_public_chart_last_sale_volume_v1"),
            }
        )
    facts.sort(key=lambda x: x["event_time"])
    return facts, {
        "provider": NASDAQ,
        "url": url,
        "raw_row_count": len(rows),
        "timestamp_parseable_count": parseable,
        "trade_date_match_count": date_match,
        "target_window_match_count": len(facts),
        "sample_raw_timestamp": sample_raw,
        "detected_timestamp_units": sorted(units),
    }


def fetch_twelve_regular(
    symbol: str,
    asset_class: str,
    trade_date: str,
    start_et: str,
    end_et: str,
    *,
    store_root: Path,
    config: Mapping[str, Any],
    access: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    spec = access["twelve_data_basic"]
    key = os.environ.get(str(spec.get("api_key_env") or "TWELVE_DATA_API_KEY"))
    if not key:
        return [], {"provider": TWELVE, "status": "secret_missing"}
    budget = config["collector"]["budgets"]["twelve_data_basic"]
    base.wait_for_twelve_budget(
        store_root,
        trade_date,
        per_day=int(budget["hard_credits_per_day"]),
        per_minute=int(budget["hard_credits_per_minute"]),
    )
    params = urllib.parse.urlencode(
        {
            "symbol": symbol,
            "interval": str(spec.get("interval_intraday") or "1min"),
            "start_date": f"{trade_date} {start_et}:00",
            "end_date": f"{trade_date} {end_et}:00",
            "timezone": str(spec.get("intraday_timezone") or "America/New_York"),
            "prepost": "false",
            "order": "asc",
            "outputsize": int(spec.get("intraday_outputsize") or 90),
            "format": "JSON",
            "apikey": key,
        }
    )
    url = f"{spec['base_url']}{spec['time_series_path']}?{params}"
    payload = base._http_json(
        url,
        timeout=int(access["http"]["timeout_seconds"]),
        user_agent=str(access["http"]["user_agent"]),
    )
    base.consume_twelve_credit(
        store_root,
        trade_date,
        amount=1,
        per_day=int(budget["hard_credits_per_day"]),
        per_minute=int(budget["hard_credits_per_minute"]),
    )
    if payload.get("status") == "error":
        raise RuntimeError(str(payload.get("message") or "Twelve Data error"))
    start, end = _window_bounds(trade_date, start_et, end_et)
    facts: list[dict[str, Any]] = []
    parseable = 0
    date_match = 0
    for row in payload.get("values") or []:
        if not isinstance(row, dict):
            continue
        raw_dt = str(row.get("datetime") or "")
        try:
            ts = datetime.strptime(raw_dt, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ET)
        except ValueError:
            continue
        parseable += 1
        if ts.date().isoformat() != trade_date:
            continue
        date_match += 1
        if not (start <= ts < end):
            continue
        close = _number(row.get("close"))
        if close is None:
            continue
        fact: dict[str, Any] = {
            "symbol": symbol,
            "asset_class": asset_class,
            "session": "regular",
            "event_time": ts.isoformat(),
            "source_timestamp": ts.isoformat(),
            "open": _number(row.get("open")),
            "high": _number(row.get("high")),
            "low": _number(row.get("low")),
            "close": close,
            "volume": max(_number(row.get("volume")) or 0.0, 0.0),
            "source_contract": "twelve_data_basic_regular_1min_v1",
        }
        facts.append({k: v for k, v in fact.items() if v is not None})
    facts.sort(key=lambda x: x["event_time"])
    return facts, {
        "provider": TWELVE,
        "raw_row_count": len(payload.get("values") or []),
        "timestamp_parseable_count": parseable,
        "trade_date_match_count": date_match,
        "target_window_match_count": len(facts),
        "feed_scope": "twelve_data_basic_us_equities_limited_realtime_coverage",
    }


def _close_state_path(store_root: Path, trade_date: str) -> Path:
    return store_root / "collector-state" / trade_date[:7] / f"{trade_date}-close-missing.json"


def _write_close_state(store_root: Path, trade_date: str, mode: str, missing: Sequence[str]) -> None:
    path = _close_state_path(store_root, trade_date)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "trade_date": trade_date,
        "updated_at": datetime.now(ET).isoformat(),
        "last_mode": mode,
        "missing": sorted(set(str(s) for s in missing)),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")


def _read_close_state(store_root: Path, trade_date: str) -> list[str] | None:
    path = _close_state_path(store_root, trade_date)
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("trade_date") != trade_date or not isinstance(value.get("missing"), list):
        raise RuntimeError(f"invalid close missing state: {path}")
    return [str(s) for s in value["missing"]]


def _actual_cutoff(store_root: Path, refs: Sequence[Mapping[str, Any]]) -> str | None:
    return base._actual_cutoff_from_refs(store_root, list(refs))


def _decorate_context_facts(universe: Mapping[str, Any], facts: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [collection.decorate_fact(universe, fact) for fact in facts]


def collect_regular_window(
    *,
    mode: str,
    stage: str | None,
    trade_date: str,
    start_et: str,
    end_et: str,
    store_root: Path,
    universe_config: Mapping[str, Any],
    config: Mapping[str, Any],
    access: Mapping[str, Any],
    symbols_override: Sequence[str] | None = None,
) -> dict[str, Any]:
    full_universe = collection.intraday_universe(universe_config)
    by_symbol = dict(full_universe)
    if symbols_override is None:
        universe = full_universe
    else:
        unknown = sorted(set(str(symbol).upper() for symbol in symbols_override) - set(by_symbol))
        if unknown:
            raise RuntimeError(f"symbols_override outside collection universe: {unknown}")
        universe = [(str(symbol).upper(), by_symbol[str(symbol).upper()]) for symbol in symbols_override]

    generated = datetime.now(ET).isoformat()
    facts_by_symbol: dict[str, tuple[str, list[dict[str, Any]]]] = {}
    diagnostics: dict[str, list[dict[str, Any]]] = {}
    failures: dict[str, list[str]] = {}

    def nasdaq_job(symbol: str, asset: str):
        return symbol, fetch_nasdaq_regular(symbol, asset, trade_date, start_et, end_et, access)

    workers = max(1, int(access["nasdaq_public_intraday"].get("max_workers") or 4))
    with ThreadPoolExecutor(max_workers=min(workers, max(1, len(universe)))) as pool:
        futures = {pool.submit(nasdaq_job, symbol, asset): symbol for symbol, asset in universe}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                _symbol, (facts, diag) = future.result()
                diagnostics.setdefault(symbol, []).append(diag)
                if facts:
                    facts_by_symbol[symbol] = (NASDAQ, _decorate_context_facts(universe_config, facts))
                else:
                    failures.setdefault(symbol, []).append("nasdaq_no_qualified_target_window_facts")
            except Exception as exc:  # noqa: BLE001
                failures.setdefault(symbol, []).append(f"nasdaq:{type(exc).__name__}:{exc}")

    for symbol, asset in universe:
        if symbol in facts_by_symbol:
            continue
        try:
            facts, diag = fetch_twelve_regular(
                symbol,
                asset,
                trade_date,
                start_et,
                end_et,
                store_root=store_root,
                config=config,
                access=access,
            )
            diagnostics.setdefault(symbol, []).append(diag)
            if facts:
                facts_by_symbol[symbol] = (TWELVE, _decorate_context_facts(universe_config, facts))
            else:
                failures.setdefault(symbol, []).append("twelve_no_qualified_target_window_facts")
        except Exception as exc:  # noqa: BLE001
            failures.setdefault(symbol, []).append(f"twelve:{type(exc).__name__}:{exc}")

    context_specs = collection.context_by_symbol(universe_config)
    capture_refs: list[dict[str, Any]] = []
    missing: list[str] = []
    provider_counts: dict[str, int] = {}
    successful: set[str] = set()
    for symbol, _asset in universe:
        selected = facts_by_symbol.get(symbol)
        if selected is None:
            missing.append(symbol)
            continue
        successful.add(symbol)
        provider, facts = selected
        provider_counts[provider] = provider_counts.get(provider, 0) + 1
        feed_scope = (
            "nasdaq_public_chart_last_sale_volume_v1"
            if provider == NASDAQ
            else "twelve_data_basic_us_equities_limited_realtime_coverage"
        )
        rel, blob = write_capture(
            store_root,
            trade_date=trade_date,
            session="regular",
            provider=provider,
            capture_id=f"{mode}-{symbol.lower()}-{datetime.now(ET).strftime('%H%M%S-et')}",
            generated_at=generated,
            actual_data_cutoff=facts[-1]["source_timestamp"],
            window={"start": start_et, "end": end_et},
            feed_scope=feed_scope,
            qualified_facts=facts,
            missing_symbols=[],
        )
        capture_refs.append(
            {
                "path": rel,
                "blob_sha": blob,
                "kind": "cross_asset_proxy_capture" if symbol in context_specs else "regular_intraday_capture",
                "window": f"{start_et}-{end_et}",
                "provider": provider,
            }
        )

    stage_name = stage or mode
    if stage_name == "open_30m":
        prior_stage = "open_15m"
    elif stage_name == "open_60m":
        prior_stage = "open_30m"
    elif stage_name == "close" and mode == "close":
        prior_stage = "open_60m"
    elif stage_name == "close" and mode in ("close_retry", "close_final", HISTORICAL_CONTEXT_REPAIR):
        prior_stage = "close"
    else:
        prior_stage = None

    prior_refs: list[dict[str, Any]] = []
    prior_missing: list[str] = []
    if prior_stage:
        prior_refs, prior_missing = base._load_prior_snapshot(store_root, trade_date, prior_stage)
        if stage_name in ("open_30m", "open_60m") and not prior_refs and not prior_missing:
            prior_missing = [symbol for symbol, _asset in full_universe]

    if stage_name in ("open_30m", "open_60m"):
        snapshot_missing = sorted(set(prior_missing) | set(missing))
    elif stage_name == "close" and mode in ("close_retry", "close_final", HISTORICAL_CONTEXT_REPAIR):
        snapshot_missing = sorted((set(prior_missing) - successful) | set(missing))
    else:
        snapshot_missing = sorted(set(missing))

    if stage_name == "close" and mode in ("close", "close_retry", "close_final"):
        _write_close_state(store_root, trade_date, mode, snapshot_missing)

    if not capture_refs:
        return {
            "mode": mode,
            "status": "no_new_qualified_facts",
            "symbols_requested_in_increment": len(universe),
            "symbols_missing_in_increment": len(missing),
            "missing": missing,
            "snapshot_missing": snapshot_missing,
            "provider_counts": provider_counts,
            "failures": failures,
            "diagnostics": diagnostics,
            "snapshot_written": False,
        }

    all_refs = prior_refs + capture_refs
    sid = f"{mode}-{datetime.now(ET).strftime('%H%M%S-et')}"
    target_start = "09:30" if stage_name.startswith("open_") else start_et
    write_snapshot(
        store_root,
        stage=stage_name,
        trade_date=trade_date,
        snapshot_id=sid,
        generated_at=generated,
        data_refs=all_refs,
        coverage={
            "symbols_requested_in_increment": len(universe),
            "symbols_available_in_increment": len(capture_refs),
            "symbols_missing_in_increment": len(missing),
            "inherited_ref_count": len(prior_refs),
            "total_ref_count": len(all_refs),
            "provider_counts": provider_counts,
        },
        missing=snapshot_missing,
        target_window={"start": target_start, "end": end_et},
        actual_data_cutoff=_actual_cutoff(store_root, all_refs),
    )
    return {
        "mode": mode,
        "status": "ok" if not snapshot_missing else "partial",
        "symbols_requested_in_increment": len(universe),
        "symbols_available_in_increment": len(capture_refs),
        "symbols_missing_in_increment": len(missing),
        "missing": missing,
        "snapshot_missing": snapshot_missing,
        "provider_counts": provider_counts,
        "failures": failures,
        "diagnostics": diagnostics,
        "refs": len(all_refs),
        "snapshot_written": True,
        "context_categories_requested": sorted(
            {context_specs[symbol]["category"] for symbol, _asset in universe if symbol in context_specs}
        ),
    }


def _retry_symbols(store_root: Path, trade_date: str, universe_config: Mapping[str, Any]) -> list[str]:
    state = _read_close_state(store_root, trade_date)
    if state is not None:
        return state
    refs, prior_missing = base._load_prior_snapshot(store_root, trade_date, "close")
    if refs or prior_missing:
        return prior_missing
    return [symbol for symbol, _asset in collection.intraday_universe(universe_config)]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        required=True,
        choices=[
            "previous_session_eod",
            "premarket",
            "open_15m",
            "open_30m",
            "open_60m",
            "close",
            "close_retry",
            "close_final",
            HISTORICAL_CONTEXT_REPAIR,
        ],
    )
    parser.add_argument("--trade-date")
    parser.add_argument("--store-root", default="sources/market-data")
    parser.add_argument("--universe", default="config/collection-universe.json")
    parser.add_argument("--config", default="config/market-data-store.yaml")
    parser.add_argument("--access-config", default="config/market-data-collector-access.yaml")
    parser.add_argument("--maintenance-authorized", action="store_true")
    args = parser.parse_args(argv)

    trade_date = args.trade_date or datetime.now(ET).strftime("%Y-%m-%d")
    store_root = Path(args.store_root)
    universe_config = collection.load_collection_universe(args.universe)
    config = base.load_yaml(args.config)
    access = base.load_yaml(args.access_config)

    if args.mode == HISTORICAL_CONTEXT_REPAIR and not args.maintenance_authorized:
        raise RuntimeError("historical_context_repair requires --maintenance-authorized")

    if args.mode == "previous_session_eod":
        # The old provider primitive still consumes the legacy shape, but it is
        # derived in memory from the first-class universe. No compatibility file
        # participates in the collection path.
        watchlist, completeness = collection.compatibility_views(universe_config)
        result = base.collect_previous_session_eod(
            trade_date=trade_date,
            store_root=store_root,
            watchlist=watchlist,
            completeness=completeness,
            config=config,
            access=access,
        )
    elif args.mode == "open_15m":
        result = collect_regular_window(mode=args.mode, stage=None, trade_date=trade_date, start_et="09:30", end_et="09:45", store_root=store_root, universe_config=universe_config, config=config, access=access)
    elif args.mode == "open_30m":
        result = collect_regular_window(mode=args.mode, stage=None, trade_date=trade_date, start_et="09:45", end_et="10:00", store_root=store_root, universe_config=universe_config, config=config, access=access)
    elif args.mode == "open_60m":
        result = collect_regular_window(mode=args.mode, stage=None, trade_date=trade_date, start_et="10:00", end_et="10:30", store_root=store_root, universe_config=universe_config, config=config, access=access)
    elif args.mode in ("close", "close_retry", "close_final"):
        symbols_override = None
        if args.mode in ("close_retry", "close_final"):
            symbols_override = _retry_symbols(store_root, trade_date, universe_config)
            if not symbols_override:
                print(json.dumps({"mode": args.mode, "status": "nothing_missing", "changed": 0}, sort_keys=True))
                return 0
        result = collect_regular_window(mode=args.mode, stage="close", trade_date=trade_date, start_et="15:45", end_et="16:00", store_root=store_root, universe_config=universe_config, config=config, access=access, symbols_override=symbols_override)
        result["terminal_semantics"] = "timestamped_regular_session_context_provider_specific_not_official_close_or_sip"
    elif args.mode == HISTORICAL_CONTEXT_REPAIR:
        symbols_override = collection.context_symbols(universe_config)
        result = collect_regular_window(
            mode=args.mode,
            stage="close",
            trade_date=trade_date,
            start_et="15:45",
            end_et="16:00",
            store_root=store_root,
            universe_config=universe_config,
            config=config,
            access=access,
            symbols_override=symbols_override,
        )
        result["repair_scope"] = "cutoff_valid_cross_asset_context_proxies_only"
        result["terminal_semantics"] = "timestamped_regular_session_proxy_context_not_formal_underlying_metric"
    else:
        result = {"mode": args.mode, "status": "no_qualified_live_premarket_adapter_registered", "changed": 0}

    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
