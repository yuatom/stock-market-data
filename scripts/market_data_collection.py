#!/usr/bin/env python3
"""Canonical market-data collection orchestration.

Provider primitives remain in market_data_collectors.py.  This module owns the
current regular-session orchestration fixes proven necessary by the 2026-08-14
live run: robust Nasdaq timestamp parsing, structured Twelve Data fallback, and
Close missing-state persistence independent of Stage Snapshot creation.
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

import market_data_collectors as base
from market_data_store import write_capture, write_snapshot

ET = ZoneInfo("America/New_York")
NASDAQ = "nasdaq_public_intraday"
TWELVE = "twelve_data_basic"


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


def collect_regular_window(
    *,
    mode: str,
    stage: str | None,
    trade_date: str,
    start_et: str,
    end_et: str,
    store_root: Path,
    watchlist: Mapping[str, Any],
    completeness: Mapping[str, Any],
    config: Mapping[str, Any],
    access: Mapping[str, Any],
    symbols_override: Sequence[str] | None = None,
) -> dict[str, Any]:
    full_universe = base._intraday_universe(watchlist, completeness)
    by_symbol = dict(full_universe)
    if symbols_override is None:
        universe = full_universe
    else:
        universe = [(symbol, by_symbol[symbol]) for symbol in symbols_override if symbol in by_symbol]

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
                    facts_by_symbol[symbol] = (NASDAQ, facts)
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
                facts_by_symbol[symbol] = (TWELVE, facts)
            else:
                failures.setdefault(symbol, []).append("twelve_no_qualified_target_window_facts")
        except Exception as exc:  # noqa: BLE001
            failures.setdefault(symbol, []).append(f"twelve:{type(exc).__name__}:{exc}")

    capture_refs: list[dict[str, Any]] = []
    missing: list[str] = []
    provider_counts: dict[str, int] = {}
    for symbol, _asset in universe:
        selected = facts_by_symbol.get(symbol)
        if selected is None:
            missing.append(symbol)
            continue
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
        capture_refs.append({"path": rel, "blob_sha": blob, "kind": "regular_intraday_capture", "window": f"{start_et}-{end_et}", "provider": provider})

    stage_name = stage or mode
    if stage_name == "open_30m":
        prior_stage = "open_15m"
    elif stage_name == "open_60m":
        prior_stage = "open_30m"
    elif stage_name == "close" and mode == "close":
        prior_stage = "open_60m"
    elif stage_name == "close" and mode in ("close_retry", "close_final"):
        prior_stage = "close"
    else:
        prior_stage = None

    prior_refs: list[dict[str, Any]] = []
    prior_missing: list[str] = []
    if prior_stage:
        prior_refs, prior_missing = base._load_prior_snapshot(store_root, trade_date, prior_stage)
        if stage_name in ("open_30m", "open_60m") and not prior_refs and not prior_missing:
            prior_missing = [symbol for symbol, _asset in full_universe]

    if stage_name == "close":
        _write_close_state(store_root, trade_date, mode, missing)

    if not capture_refs:
        return {
            "mode": mode,
            "status": "no_new_qualified_facts",
            "symbols_requested_in_increment": len(universe),
            "symbols_missing_in_increment": len(missing),
            "missing": missing,
            "provider_counts": provider_counts,
            "failures": failures,
            "diagnostics": diagnostics,
            "snapshot_written": False,
        }

    all_refs = prior_refs + capture_refs
    if stage_name in ("open_30m", "open_60m"):
        snapshot_missing = sorted(set(prior_missing) | set(missing))
    else:
        snapshot_missing = sorted(set(missing))
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
        "provider_counts": provider_counts,
        "failures": failures,
        "diagnostics": diagnostics,
        "refs": len(all_refs),
        "snapshot_written": True,
    }


def _retry_symbols(
    store_root: Path,
    trade_date: str,
    watchlist: Mapping[str, Any],
    completeness: Mapping[str, Any],
) -> list[str]:
    state = _read_close_state(store_root, trade_date)
    if state is not None:
        return state
    refs, prior_missing = base._load_prior_snapshot(store_root, trade_date, "close")
    if refs or prior_missing:
        return prior_missing
    return [symbol for symbol, _asset in base._intraday_universe(watchlist, completeness)]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", required=True, choices=["previous_session_eod", "premarket", "open_15m", "open_30m", "open_60m", "close", "close_retry", "close_final"])
    parser.add_argument("--trade-date")
    parser.add_argument("--store-root", default="sources/market-data")
    parser.add_argument("--watchlist", default="config/watchlist.json")
    parser.add_argument("--config", default="config/market-data-store.yaml")
    parser.add_argument("--access-config", default="config/market-data-collector-access.yaml")
    parser.add_argument("--data-completeness", default="config/data-completeness.yaml")
    args = parser.parse_args(argv)

    trade_date = args.trade_date or datetime.now(ET).strftime("%Y-%m-%d")
    store_root = Path(args.store_root)
    watchlist = base.load_json(args.watchlist)
    config = base.load_yaml(args.config)
    access = base.load_yaml(args.access_config)
    completeness = base.load_yaml(args.data_completeness)

    if args.mode == "previous_session_eod":
        result = base.collect_previous_session_eod(trade_date=trade_date, store_root=store_root, watchlist=watchlist, completeness=completeness, config=config, access=access)
    elif args.mode == "open_15m":
        result = collect_regular_window(mode=args.mode, stage=None, trade_date=trade_date, start_et="09:30", end_et="09:45", store_root=store_root, watchlist=watchlist, completeness=completeness, config=config, access=access)
    elif args.mode == "open_30m":
        result = collect_regular_window(mode=args.mode, stage=None, trade_date=trade_date, start_et="09:45", end_et="10:00", store_root=store_root, watchlist=watchlist, completeness=completeness, config=config, access=access)
    elif args.mode == "open_60m":
        result = collect_regular_window(mode=args.mode, stage=None, trade_date=trade_date, start_et="10:00", end_et="10:30", store_root=store_root, watchlist=watchlist, completeness=completeness, config=config, access=access)
    elif args.mode in ("close", "close_retry", "close_final"):
        symbols_override = None
        if args.mode in ("close_retry", "close_final"):
            symbols_override = _retry_symbols(store_root, trade_date, watchlist, completeness)
            if not symbols_override:
                print(json.dumps({"mode": args.mode, "status": "nothing_missing", "changed": 0}, sort_keys=True))
                return 0
        result = collect_regular_window(mode=args.mode, stage="close", trade_date=trade_date, start_et="15:45", end_et="16:00", store_root=store_root, watchlist=watchlist, completeness=completeness, config=config, access=access, symbols_override=symbols_override)
        result["terminal_semantics"] = "timestamped_regular_session_context_provider_specific_not_official_close_or_sip"
    else:
        result = {"mode": args.mode, "status": "no_qualified_live_premarket_adapter_registered", "changed": 0}

    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
