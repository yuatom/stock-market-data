#!/usr/bin/env python3
"""Deterministic free-source market-data collectors.

This module is the implementation behind collect_market_data.py. It never owns
report research, decision, projection, or canonical report finalization.
"""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

import yaml

try:
    from market_data_store import (
        MarketDataConflict,
        append_daily_bars,
        git_blob_sha_bytes,
        read_daily_series,
        write_capture,
        write_snapshot,
    )
except ImportError:  # pragma: no cover
    from scripts.market_data_store import (
        MarketDataConflict,
        append_daily_bars,
        git_blob_sha_bytes,
        read_daily_series,
        write_capture,
        write_snapshot,
    )

ET = ZoneInfo("America/New_York")
TWELVE_PROVIDER = "twelve_data_basic"
NASDAQ_PROVIDER = "nasdaq_public_intraday"


class BudgetExceeded(RuntimeError):
    pass


class StoreIntegrityError(RuntimeError):
    pass


def load_yaml(path: str | Path) -> dict[str, Any]:
    value = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected mapping: {path}")
    return value


def load_json(path: str | Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected object: {path}")
    return value


def _http_json(url: str, *, timeout: int, user_agent: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status}")
        content_type = response.headers.get("content-type", "")
        if "json" not in content_type.lower():
            raise RuntimeError(f"non-json response: {content_type}")
        value = json.loads(response.read().decode("utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("JSON response is not an object")
    return value


def _budget_path(store_root: Path, trade_date: str) -> Path:
    return store_root / "collector-state" / trade_date[:7] / f"{trade_date}-twelve-data-budget.json"


def _load_budget_state(path: Path, trade_date: str) -> dict[str, Any]:
    if path.exists():
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("trade_date") != trade_date:
            raise RuntimeError(f"invalid budget state: {path}")
        return value
    return {"schema_version": 1, "trade_date": trade_date, "credits": 0, "minutes": {}}


def wait_for_twelve_budget(
    store_root: Path,
    trade_date: str,
    *,
    per_day: int,
    per_minute: int,
) -> None:
    while True:
        path = _budget_path(store_root, trade_date)
        now = datetime.now(timezone.utc)
        minute = now.strftime("%Y-%m-%dT%H:%MZ")
        state = _load_budget_state(path, trade_date)
        if int(state.get("credits", 0)) >= per_day:
            raise BudgetExceeded(f"Twelve Data daily budget exhausted: {per_day}")
        if int((state.get("minutes") or {}).get(minute, 0)) < per_minute:
            return
        time.sleep(max(1.0, 61.0 - now.second - now.microsecond / 1_000_000.0))


def consume_twelve_credit(
    store_root: Path,
    trade_date: str,
    *,
    amount: int,
    per_day: int,
    per_minute: int,
) -> None:
    path = _budget_path(store_root, trade_date)
    now = datetime.now(timezone.utc)
    minute = now.strftime("%Y-%m-%dT%H:%MZ")
    state = _load_budget_state(path, trade_date)
    day_after = int(state.get("credits", 0)) + amount
    minute_after = int((state.get("minutes") or {}).get(minute, 0)) + amount
    if day_after > per_day:
        raise BudgetExceeded(f"Twelve Data daily budget exceeded: {day_after}>{per_day}")
    if minute_after > per_minute:
        raise BudgetExceeded(f"Twelve Data minute budget exceeded: {minute_after}>{per_minute}")
    state["credits"] = day_after
    state.setdefault("minutes", {})[minute] = minute_after
    state["minutes"] = dict(sorted(state["minutes"].items())[-16:])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _asset_class(watchlist: Mapping[str, Any], symbol: str, default: str = "etf") -> str:
    meta = (watchlist.get("instruments", {}) or {}).get(symbol, {}) or {}
    return str(meta.get("asset_class") or default)


def _intraday_universe(
    watchlist: Mapping[str, Any],
    completeness: Mapping[str, Any],
) -> list[tuple[str, str]]:
    symbols: dict[str, str] = {}
    for symbol in watchlist.get("core_watchlist", []) or []:
        symbols[str(symbol).upper()] = _asset_class(watchlist, str(symbol), "stocks")
    benchmarks = ((completeness.get("minute_matrix_requirements") or {}).get("tracked_benchmarks") or [])
    for symbol in benchmarks:
        symbols.setdefault(str(symbol).upper(), _asset_class(watchlist, str(symbol), "etf"))
    return sorted(symbols.items())


def _daily_universe(
    watchlist: Mapping[str, Any],
    completeness: Mapping[str, Any],
) -> list[tuple[str, str]]:
    symbols = dict(_intraday_universe(watchlist, completeness))
    sectors = ((completeness.get("sector_close_capability") or {}).get("exact_sector_etfs") or [])
    for symbol in sectors:
        symbols.setdefault(str(symbol).upper(), _asset_class(watchlist, str(symbol), "etf"))
    return sorted(symbols.items())


def _identity_effective_date(watchlist: Mapping[str, Any], symbol: str) -> str | None:
    meta = (watchlist.get("instruments", {}) or {}).get(symbol, {}) or {}
    value = meta.get("ticker_effective_at")
    return str(value) if value else None


def fetch_twelve_daily(
    symbol: str,
    api_key: str,
    access: Mapping[str, Any],
    *,
    outputsize: int,
) -> list[dict[str, Any]]:
    spec = access["twelve_data_basic"]
    params = urllib.parse.urlencode(
        {
            "symbol": symbol,
            "interval": spec["interval_daily"],
            "outputsize": outputsize,
            "apikey": api_key,
            "format": "JSON",
        }
    )
    url = f"{spec['base_url']}{spec['time_series_path']}?{params}"
    payload = _http_json(
        url,
        timeout=int(access["http"]["timeout_seconds"]),
        user_agent=str(access["http"]["user_agent"]),
    )
    if payload.get("status") == "error":
        raise RuntimeError(str(payload.get("message") or "Twelve Data error"))
    bars: list[dict[str, Any]] = []
    for row in payload.get("values") or []:
        try:
            bar = {
                "trade_date": str(row["datetime"])[:10],
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]) if row.get("volume") not in (None, "") else 0.0,
            }
        except (KeyError, TypeError, ValueError):
            continue
        bars.append(bar)
    return sorted(bars, key=lambda x: x["trade_date"])


def _series_index_path(store_root: Path, provider: str, symbol: str) -> Path:
    return store_root / "series" / "daily" / provider / symbol.upper() / "_index.json"


def collect_previous_session_eod(
    *,
    trade_date: str,
    store_root: Path,
    watchlist: Mapping[str, Any],
    completeness: Mapping[str, Any],
    config: Mapping[str, Any],
    access: Mapping[str, Any],
) -> dict[str, Any]:
    key = os.environ.get("TWELVE_DATA_API_KEY")
    if not key:
        return {
            "mode": "previous_session_eod",
            "status": "secret_missing",
            "changed_series": 0,
            "bootstrapped_series": 0,
            "failures": [],
        }

    budget = config["collector"]["budgets"]["twelve_data_basic"]
    outputsize = int(config["collector"]["twelve_data_daily"]["history_outputsize"])
    bootstrap_minimum = int(config["collector"]["twelve_data_daily"]["bootstrap_minimum_records"])

    changed_series = 0
    bootstrapped_series = 0
    failures: list[dict[str, Any]] = []
    universe = _daily_universe(watchlist, completeness)

    for symbol, asset_class in universe:
        try:
            wait_for_twelve_budget(
                store_root,
                trade_date,
                per_day=int(budget["hard_credits_per_day"]),
                per_minute=int(budget["hard_credits_per_minute"]),
            )
            consume_twelve_credit(
                store_root,
                trade_date,
                amount=1,
                per_day=int(budget["hard_credits_per_day"]),
                per_minute=int(budget["hard_credits_per_minute"]),
            )
            fetched = fetch_twelve_daily(symbol, key, access, outputsize=outputsize)
            effective = _identity_effective_date(watchlist, symbol)
            eligible = [
                bar
                for bar in fetched
                if bar["trade_date"] < trade_date
                and (effective is None or bar["trade_date"] >= effective)
            ]
            if not eligible:
                failures.append({"symbol": symbol, "reason": "no_eligible_previous_session_daily_rows"})
                continue

            index_path = _series_index_path(store_root, TWELVE_PROVIDER, symbol)
            existing: list[dict[str, Any]] = []
            if index_path.exists():
                existing = read_daily_series(store_root, provider=TWELVE_PROVIDER, symbol=symbol)
                existing_by_date = {row["trade_date"]: row for row in existing}
                for row in eligible:
                    old = existing_by_date.get(row["trade_date"])
                    if old is not None and old != row:
                        raise MarketDataConflict(
                            f"provider historical revision detected for {symbol} {row['trade_date']}"
                        )
            elif len(eligible) < bootstrap_minimum:
                failures.append(
                    {
                        "symbol": symbol,
                        "reason": "insufficient_bootstrap_history",
                        "records": len(eligible),
                    }
                )
                continue

            last_date = existing[-1]["trade_date"] if existing else None
            new_rows = [row for row in eligible if last_date is None or row["trade_date"] > last_date]
            if not new_rows:
                continue

            result = append_daily_bars(
                store_root,
                provider=TWELVE_PROVIDER,
                symbol=symbol,
                asset_class=asset_class,
                bars=new_rows,
                identity_effective_from=effective,
                series_semantics="daily_regular_ohlcv",
                adjustment_semantics="provider_reported",
                lineage={
                    "ingest_kind": "collector_previous_session_eod",
                    "ingest_trade_date": trade_date,
                    "history_outputsize": outputsize,
                },
            )
            if result.changed:
                changed_series += 1
                if not existing:
                    bootstrapped_series += 1
        except BudgetExceeded:
            raise
        except Exception as exc:
            failures.append({"symbol": symbol, "reason": type(exc).__name__, "detail": str(exc)[:240]})

    return {
        "mode": "previous_session_eod",
        "status": "ok" if not failures else "partial",
        "symbols_requested": len(universe),
        "changed_series": changed_series,
        "bootstrapped_series": bootstrapped_series,
        "failures": failures,
    }


def fetch_nasdaq_regular(
    symbol: str,
    asset_class: str,
    trade_date: str,
    start_et: str,
    end_et: str,
    access: Mapping[str, Any],
) -> list[dict[str, Any]]:
    spec = access["nasdaq_public_intraday"]
    url = spec["regular_session_endpoint_template"].format(
        symbol=symbol.upper(),
        asset_class=asset_class,
    )
    payload = _http_json(
        url,
        timeout=int(access["http"]["timeout_seconds"]),
        user_agent=str(access["http"]["user_agent"]),
    )
    chart = ((payload.get("data") or {}).get("chart") or [])
    points_by_minute: dict[str, dict[str, Any]] = {}
    for point in chart:
        try:
            ts = datetime.fromtimestamp(int(point["x"]) / 1000, tz=timezone.utc).astimezone(ET)
            price = float(str(point["y"]).replace("$", "").replace(",", ""))
            volume = float(str(point.get("w", 0)).replace(",", ""))
        except (KeyError, TypeError, ValueError):
            continue
        if ts.strftime("%Y-%m-%d") != trade_date:
            continue
        hm = ts.strftime("%H:%M")
        if not (start_et <= hm < end_et):
            continue
        minute = ts.replace(second=0, microsecond=0).isoformat()
        candidate = {
            "symbol": symbol.upper(),
            "asset_class": asset_class,
            "session": "regular",
            "event_time": ts.isoformat(),
            "source_timestamp": ts.isoformat(),
            "last_sale": price,
            "reported_volume": volume,
        }
        prior = points_by_minute.get(minute)
        if prior is not None and prior != candidate:
            raise RuntimeError(f"multiple conflicting Nasdaq points in minute {symbol} {minute}")
        points_by_minute[minute] = candidate
    return [points_by_minute[key] for key in sorted(points_by_minute)]


def _verify_local_blob(store_root: Path, rel_path: str, expected_sha: str) -> dict[str, Any]:
    path = store_root / rel_path
    raw = path.read_bytes()
    actual = git_blob_sha_bytes(raw)
    if actual != expected_sha:
        raise StoreIntegrityError(f"blob mismatch {rel_path}: {actual} != {expected_sha}")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise StoreIntegrityError(f"expected object {rel_path}")
    return value


def _load_prior_snapshot(
    store_root: Path,
    trade_date: str,
    stage: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    latest_path = store_root / "snapshots" / trade_date[:7] / trade_date / stage / "latest.json"
    if not latest_path.exists():
        return [], []
    latest = json.loads(latest_path.read_text(encoding="utf-8"))
    snapshot = _verify_local_blob(
        store_root,
        str(latest["snapshot_path"]),
        str(latest["snapshot_blob_sha"]),
    )
    refs = [dict(item) for item in snapshot.get("data_refs") or []]
    for ref in refs:
        _verify_local_blob(store_root, str(ref["path"]), str(ref["blob_sha"]))
    missing = [str(item) for item in snapshot.get("missing") or [] if isinstance(item, str)]
    return refs, missing


def _actual_cutoff_from_refs(store_root: Path, refs: Sequence[Mapping[str, Any]]) -> str | None:
    cutoffs: list[str] = []
    for ref in refs:
        try:
            doc = _verify_local_blob(store_root, str(ref["path"]), str(ref["blob_sha"]))
        except (FileNotFoundError, json.JSONDecodeError, StoreIntegrityError):
            continue
        value = doc.get("actual_data_cutoff")
        if isinstance(value, str) and value:
            cutoffs.append(value)
    return max(cutoffs) if cutoffs else None


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
    access: Mapping[str, Any],
    symbols_override: Sequence[str] | None = None,
) -> dict[str, Any]:
    generated = datetime.now(ET).isoformat()
    universe = _intraday_universe(watchlist, completeness)
    if symbols_override is not None:
        allowed = {symbol.upper() for symbol in symbols_override}
        universe = [(symbol, asset_class) for symbol, asset_class in universe if symbol in allowed]

    max_workers = int(access["nasdaq_public_intraday"].get("max_workers", 4))
    results: dict[str, list[dict[str, Any]]] = {}
    failures: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(
                fetch_nasdaq_regular,
                symbol,
                asset_class,
                trade_date,
                start_et,
                end_et,
                access,
            ): (symbol, asset_class)
            for symbol, asset_class in universe
        }
        for future in as_completed(futures):
            symbol, _ = futures[future]
            try:
                points = future.result()
            except Exception as exc:
                points = []
                failures[symbol] = f"{type(exc).__name__}:{str(exc)[:160]}"
            results[symbol] = points

    capture_refs: list[dict[str, Any]] = []
    missing: list[str] = []
    for symbol, _asset_class_name in universe:
        points = results.get(symbol) or []
        if not points:
            missing.append(symbol)
            continue
        cid = f"{mode}-{symbol.lower()}-{datetime.now(ET).strftime('%H%M%S')}"
        rel, blob = write_capture(
            store_root,
            trade_date=trade_date,
            session="regular",
            provider=NASDAQ_PROVIDER,
            capture_id=cid,
            generated_at=generated,
            actual_data_cutoff=points[-1]["event_time"],
            window={"start": start_et, "end": end_et},
            feed_scope="nasdaq_public_chart_last_sale_volume_v1",
            qualified_facts=points,
        )
        capture_refs.append(
            {
                "path": rel,
                "blob_sha": blob,
                "kind": "regular_intraday_capture",
                "window": f"{start_et}-{end_et}",
            }
        )

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
        prior_refs, prior_missing = _load_prior_snapshot(store_root, trade_date, prior_stage)

    if not capture_refs:
        return {
            "mode": mode,
            "status": "no_new_qualified_facts",
            "symbols_requested_in_increment": len(universe),
            "symbols_missing_in_increment": len(missing),
            "missing": missing,
            "failures": failures,
            "snapshot_written": False,
        }

    all_refs = prior_refs + capture_refs
    if stage_name in ("open_30m", "open_60m"):
        snapshot_missing = sorted(set(prior_missing) | set(missing))
    elif stage_name == "close" and mode in ("close_retry", "close_final"):
        snapshot_missing = sorted(set(missing))
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
        },
        missing=snapshot_missing,
        target_window={"start": target_start, "end": end_et},
        actual_data_cutoff=_actual_cutoff_from_refs(store_root, all_refs),
    )
    return {
        "mode": mode,
        "status": "ok" if not missing else "partial",
        "symbols_requested_in_increment": len(universe),
        "symbols_available_in_increment": len(capture_refs),
        "missing": missing,
        "failures": failures,
        "refs": len(all_refs),
        "snapshot_written": True,
    }


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
        ],
    )
    parser.add_argument("--trade-date")
    parser.add_argument("--store-root", default="sources/market-data")
    parser.add_argument("--watchlist", default="config/watchlist.json")
    parser.add_argument("--config", default="config/market-data-store.yaml")
    parser.add_argument("--access-config", default="config/market-data-collector-access.yaml")
    parser.add_argument("--data-completeness", default="config/data-completeness.yaml")
    args = parser.parse_args(argv)

    trade_date = args.trade_date or datetime.now(ET).strftime("%Y-%m-%d")
    store_root = Path(args.store_root)
    watchlist = load_json(args.watchlist)
    config = load_yaml(args.config)
    access = load_yaml(args.access_config)
    completeness = load_yaml(args.data_completeness)

    if args.mode == "previous_session_eod":
        result = collect_previous_session_eod(
            trade_date=trade_date,
            store_root=store_root,
            watchlist=watchlist,
            completeness=completeness,
            config=config,
            access=access,
        )
    elif args.mode == "open_15m":
        result = collect_regular_window(
            mode="open_15m",
            stage=None,
            trade_date=trade_date,
            start_et="09:30",
            end_et="09:45",
            store_root=store_root,
            watchlist=watchlist,
            completeness=completeness,
            access=access,
        )
    elif args.mode == "open_30m":
        result = collect_regular_window(
            mode="open_30m",
            stage=None,
            trade_date=trade_date,
            start_et="09:45",
            end_et="10:00",
            store_root=store_root,
            watchlist=watchlist,
            completeness=completeness,
            access=access,
        )
    elif args.mode == "open_60m":
        result = collect_regular_window(
            mode="open_60m",
            stage=None,
            trade_date=trade_date,
            start_et="10:00",
            end_et="10:30",
            store_root=store_root,
            watchlist=watchlist,
            completeness=completeness,
            access=access,
        )
    elif args.mode in ("close", "close_retry", "close_final"):
        symbols_override = None
        if args.mode in ("close_retry", "close_final"):
            _refs, prior_missing = _load_prior_snapshot(store_root, trade_date, "close")
            if not prior_missing:
                print(
                    json.dumps(
                        {"mode": args.mode, "status": "nothing_missing", "changed": 0},
                        sort_keys=True,
                    )
                )
                return 0
            symbols_override = prior_missing
        result = collect_regular_window(
            mode=args.mode,
            stage="close",
            trade_date=trade_date,
            start_et="15:45",
            end_et="16:00",
            store_root=store_root,
            watchlist=watchlist,
            completeness=completeness,
            access=access,
            symbols_override=symbols_override,
        )
        result["terminal_semantics"] = (
            "timestamped_regular_session_last_sale_context_not_official_close"
        )
    else:
        result = {
            "mode": args.mode,
            "status": "no_qualified_live_premarket_adapter_registered",
            "changed": 0,
        }

    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
