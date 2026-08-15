#!/usr/bin/env python3
"""One-shot safe initialization of reusable daily market-data baselines.

This utility is deliberately narrower than the normal daily collector.  It may
extend an existing provider-affine series backwards only when every overlapping
record is byte-equivalent after canonical normalization.  It never revises an
existing market fact, fills an internal gap, changes provider/session semantics,
or bypasses security identity effective dates.

The target depth is derived from
config/market-data-store.yaml#series.analysis_windows_may_read_last_sessions,
so the initialization does not create a second authority for research history
length.  Once the baseline is initialized, the normal previous_session_eod
collector remains append-only.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import market_data_collectors as collectors
import market_data_store as store


class BaselineInitializationError(RuntimeError):
    pass


def _series_index_path(store_root: Path, provider: str, symbol: str) -> Path:
    return store_root / "series" / "daily" / provider / symbol.upper() / "_index.json"


def _shift_lineage(segments: Sequence[Mapping[str, Any]], offset: int) -> list[dict[str, Any]]:
    shifted: list[dict[str, Any]] = []
    for raw in segments:
        item = dict(raw)
        if isinstance(item.get("start"), int):
            item["start"] = int(item["start"]) + offset
        if isinstance(item.get("end"), int):
            item["end"] = int(item["end"]) + offset
        shifted.append(item)
    return shifted


def merge_verified_baseline(
    existing: Sequence[Mapping[str, Any]],
    fetched: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], int, int, int]:
    """Return a safe prefix/suffix extension around an immutable existing range.

    Existing rows are the authority for their dates.  Fetched rows may add only
    dates before the first existing row or after the last existing row.  Every
    overlapping date must match exactly; a fetched date inside the existing
    range that is absent locally is treated as an integrity gap and fails.
    """
    existing_rows = [dict(x) for x in existing]
    fetched_rows = [dict(x) for x in fetched]
    if not fetched_rows:
        raise BaselineInitializationError("provider returned no eligible daily rows")
    fetched_rows.sort(key=lambda x: x["trade_date"])
    if len({x["trade_date"] for x in fetched_rows}) != len(fetched_rows):
        raise BaselineInitializationError("provider baseline contains duplicate trade_date")
    if not existing_rows:
        return fetched_rows, len(fetched_rows), 0, 0

    existing_rows.sort(key=lambda x: x["trade_date"])
    existing_by_date = {x["trade_date"]: x for x in existing_rows}
    fetched_by_date = {x["trade_date"]: x for x in fetched_rows}
    first_existing = existing_rows[0]["trade_date"]
    last_existing = existing_rows[-1]["trade_date"]

    overlap_verified = 0
    for trade_date, old in existing_by_date.items():
        new = fetched_by_date.get(trade_date)
        if new is None:
            raise BaselineInitializationError(
                f"provider baseline does not cover existing row {trade_date}"
            )
        if store.canonical_bytes(old) != store.canonical_bytes(new):
            raise BaselineInitializationError(
                f"provider historical revision conflicts with existing row {trade_date}"
            )
        overlap_verified += 1

    internal_gap_dates = [
        row["trade_date"]
        for row in fetched_rows
        if first_existing <= row["trade_date"] <= last_existing
        and row["trade_date"] not in existing_by_date
    ]
    if internal_gap_dates:
        raise BaselineInitializationError(
            "existing series has internal gap(s): " + ",".join(internal_gap_dates[:8])
        )

    prefix = [row for row in fetched_rows if row["trade_date"] < first_existing]
    suffix = [row for row in fetched_rows if row["trade_date"] > last_existing]
    return prefix + existing_rows + suffix, len(prefix), len(suffix), overlap_verified


def _write_rebased_series(
    *,
    index_path: Path,
    index: dict[str, Any],
    records: Sequence[Mapping[str, Any]],
    prefix_count: int,
    suffix_count: int,
    overlap_verified: int,
    target_records: int,
    trade_date: str,
) -> None:
    sdir = index_path.parent
    old_shards = [str(meta["path"]) for meta in index.get("shards", [])]
    record_limit = int(index.get("shard_record_limit") or store.DEFAULT_RECORD_LIMIT)
    byte_limit = int(index.get("shard_byte_limit") or store.DEFAULT_BYTE_LIMIT)
    series_id = str(index["series_id"])

    new_docs: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    cursor = 0
    rows = [dict(x) for x in records]
    while cursor < len(rows):
        start = cursor
        take: list[dict[str, Any]] = []
        while cursor < len(rows):
            candidate = take + [rows[cursor]]
            doc = store._shard_document(series_id, start, candidate)
            if len(candidate) <= record_limit and len(store.canonical_bytes(doc)) <= byte_limit:
                take.append(rows[cursor])
                cursor += 1
                continue
            if not take:
                raise BaselineInitializationError("single daily row exceeds shard byte limit")
            break
        name = f"{start:06d}.json"
        doc = store._shard_document(series_id, start, take)
        sealed = cursor < len(rows)
        meta = store._shard_meta(name, start, doc, sealed)
        new_docs.append((name, doc, meta))

    for name in old_shards:
        path = sdir / name
        if path.exists():
            path.unlink()
    for name, doc, _meta in new_docs:
        store._write_json(sdir / name, doc)

    old_lineage = _shift_lineage(index.get("lineage_segments", []) or [], prefix_count)
    lineage: list[dict[str, Any]] = []
    if prefix_count:
        lineage.append(
            {
                "ingest_kind": "collector_daily_baseline_prefix_initialization",
                "ingest_trade_date": trade_date,
                "history_outputsize": target_records,
                "start": 0,
                "end": prefix_count - 1,
            }
        )
    lineage.extend(old_lineage)
    if suffix_count:
        lineage.append(
            {
                "ingest_kind": "collector_daily_baseline_suffix_catchup",
                "ingest_trade_date": trade_date,
                "history_outputsize": target_records,
                "start": len(rows) - suffix_count,
                "end": len(rows) - 1,
            }
        )

    index["record_count"] = len(rows)
    index["first_trade_date"] = rows[0]["trade_date"] if rows else None
    index["last_trade_date"] = rows[-1]["trade_date"] if rows else None
    index["shards"] = [meta for _name, _doc, meta in new_docs]
    index["lineage_segments"] = lineage
    index["baseline_initialization"] = {
        "mode": "verified_prefix_extension_then_append_only",
        "target_records": target_records,
        "initialized_at": datetime.now(timezone.utc).isoformat(),
        "ingest_trade_date": trade_date,
        "prefix_records_added": prefix_count,
        "suffix_records_added": suffix_count,
        "overlap_records_verified_equal": overlap_verified,
    }
    store._write_json(index_path, index)


def initialize(
    *,
    trade_date: str,
    store_root: Path,
    watchlist_path: Path,
    completeness_path: Path,
    store_config_path: Path,
    access_path: Path,
) -> dict[str, Any]:
    watchlist = collectors.load_json(watchlist_path)
    completeness = collectors.load_yaml(completeness_path)
    config = collectors.load_yaml(store_config_path)
    access = collectors.load_yaml(access_path)
    windows = list(((config.get("series") or {}).get("analysis_windows_may_read_last_sessions") or []))
    if not windows or any(not isinstance(x, int) or x <= 0 for x in windows):
        raise BaselineInitializationError("market-data-store analysis window authority is invalid")
    target_records = max(int(x) for x in windows)

    key = os.environ.get("TWELVE_DATA_API_KEY")
    if not key:
        return {
            "mode": "daily_baseline_init",
            "status": "secret_missing",
            "target_records": target_records,
            "symbols_requested": 0,
            "results": [],
            "failures": [{"reason": "TWELVE_DATA_API_KEY_missing"}],
        }

    budget = config["collector"]["budgets"]["twelve_data_basic"]
    provider = collectors.TWELVE_PROVIDER
    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    universe = collectors._daily_universe(watchlist, completeness)
    for symbol, asset_class in universe:
        try:
            collectors.wait_for_twelve_budget(
                store_root,
                trade_date,
                per_day=int(budget["hard_credits_per_day"]),
                per_minute=int(budget["hard_credits_per_minute"]),
            )
            collectors.consume_twelve_credit(
                store_root,
                trade_date,
                amount=1,
                per_day=int(budget["hard_credits_per_day"]),
                per_minute=int(budget["hard_credits_per_minute"]),
            )
            fetched = collectors.fetch_twelve_daily(symbol, key, access, outputsize=target_records)
            effective = collectors._identity_effective_date(watchlist, symbol)
            eligible = [
                row for row in fetched
                if row["trade_date"] < trade_date
                and (effective is None or row["trade_date"] >= effective)
            ]
            index_path = _series_index_path(store_root, provider, symbol)
            if not index_path.exists():
                result = store.append_daily_bars(
                    store_root,
                    provider=provider,
                    symbol=symbol,
                    asset_class=asset_class,
                    bars=eligible,
                    identity_effective_from=effective,
                    series_semantics="daily_regular_ohlcv",
                    adjustment_semantics="provider_reported",
                    lineage={
                        "ingest_kind": "collector_daily_baseline_initialization",
                        "ingest_trade_date": trade_date,
                        "history_outputsize": target_records,
                    },
                )
                results.append(
                    {
                        "symbol": symbol,
                        "status": "initialized_new_series",
                        "record_count": result.record_count,
                        "target_records": target_records,
                        "first_trade_date": eligible[0]["trade_date"] if eligible else None,
                        "last_trade_date": eligible[-1]["trade_date"] if eligible else None,
                        "identity_limited": bool(effective and len(eligible) < target_records),
                    }
                )
                continue

            index = json.loads(index_path.read_text(encoding="utf-8"))
            existing = store.read_daily_series(store_root, provider=provider, symbol=symbol)
            combined, prefix_count, suffix_count, overlap_verified = merge_verified_baseline(existing, eligible)
            if not prefix_count and not suffix_count:
                results.append(
                    {
                        "symbol": symbol,
                        "status": "already_initialized",
                        "record_count": len(existing),
                        "target_records": target_records,
                        "first_trade_date": existing[0]["trade_date"] if existing else None,
                        "last_trade_date": existing[-1]["trade_date"] if existing else None,
                        "identity_limited": bool(effective and len(existing) < target_records),
                    }
                )
                continue
            _write_rebased_series(
                index_path=index_path,
                index=index,
                records=combined,
                prefix_count=prefix_count,
                suffix_count=suffix_count,
                overlap_verified=overlap_verified,
                target_records=target_records,
                trade_date=trade_date,
            )
            results.append(
                {
                    "symbol": symbol,
                    "status": "baseline_extended",
                    "record_count": len(combined),
                    "target_records": target_records,
                    "prefix_records_added": prefix_count,
                    "suffix_records_added": suffix_count,
                    "overlap_records_verified_equal": overlap_verified,
                    "first_trade_date": combined[0]["trade_date"],
                    "last_trade_date": combined[-1]["trade_date"],
                    "identity_limited": bool(effective and len(combined) < target_records),
                }
            )
        except collectors.BudgetExceeded:
            raise
        except Exception as exc:  # noqa: BLE001
            failures.append(
                {
                    "symbol": symbol,
                    "reason": type(exc).__name__,
                    "detail": str(exc)[:320],
                }
            )

    return {
        "mode": "daily_baseline_init",
        "status": "ok" if not failures else "partial",
        "target_records": target_records,
        "symbols_requested": len(universe),
        "results": results,
        "failures": failures,
    }


def _write_result(store_root: Path, trade_date: str, result: dict[str, Any]) -> Path:
    payload = dict(result)
    payload["schema_version"] = 1
    payload["market_fact_authority"] = False
    payload["recorded_at"] = datetime.now(timezone.utc).isoformat()
    path = store_root / "collector-state" / trade_date[:7] / f"{trade_date}-daily-baseline-init.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--store-root", default="sources/market-data")
    parser.add_argument("--watchlist", default="config/watchlist.json")
    parser.add_argument("--data-completeness", default="config/data-completeness.yaml")
    parser.add_argument("--store-config", default="config/market-data-store.yaml")
    parser.add_argument("--access-config", default="config/market-data-collector-access.yaml")
    args = parser.parse_args(argv)
    store_root = Path(args.store_root)
    result = initialize(
        trade_date=args.trade_date,
        store_root=store_root,
        watchlist_path=Path(args.watchlist),
        completeness_path=Path(args.data_completeness),
        store_config_path=Path(args.store_config),
        access_path=Path(args.access_config),
    )
    result_path = _write_result(store_root, args.trade_date, result)
    result["result_path"] = str(result_path)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    # Per-symbol conflicts are fail-closed for that series but do not discard
    # successfully initialized independent series.  The persisted result is the
    # explicit retry surface for any failures.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
