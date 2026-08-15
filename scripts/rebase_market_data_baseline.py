#!/usr/bin/env python3
"""Explicit audited rebase for provider-settled daily baseline initialization.

Normal daily-series writes remain append-only. This maintenance path exists only
for the Store contract's explicit-rebase escape hatch after a provider revises
already-persisted recent settlement rows. It archives the entire old logical
series, records field-level overlap revisions, requires those revisions to be
confined to a small tail window, and then rebuilds the same provider-affine
series from the provider's current qualified baseline.
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


class ExplicitRebaseError(RuntimeError):
    pass


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ExplicitRebaseError(f"{path} must contain an object")
    return value


def _series_index_path(store_root: Path, provider: str, symbol: str) -> Path:
    return store_root / "series" / "daily" / provider / symbol.upper() / "_index.json"


def _field_diff(old: Mapping[str, Any], new: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    changed: dict[str, dict[str, Any]] = {}
    for key in sorted(set(old) | set(new)):
        if old.get(key) != new.get(key):
            changed[str(key)] = {"old": old.get(key), "new": new.get(key)}
    return changed


def validate_replacement(
    existing: Sequence[Mapping[str, Any]],
    replacement: Sequence[Mapping[str, Any]],
    *,
    maximum_revised_overlap_records: int,
    revised_overlap_must_be_within_last_existing_sessions: int,
) -> list[dict[str, Any]]:
    if not existing:
        raise ExplicitRebaseError("explicit rebase requires an existing series")
    if not replacement:
        raise ExplicitRebaseError("replacement baseline is empty")

    existing_rows = [dict(x) for x in existing]
    replacement_rows = [dict(x) for x in replacement]
    existing_rows.sort(key=lambda x: x["trade_date"])
    replacement_rows.sort(key=lambda x: x["trade_date"])
    if len({x["trade_date"] for x in replacement_rows}) != len(replacement_rows):
        raise ExplicitRebaseError("replacement baseline contains duplicate trade_date")

    replacement_by_date = {row["trade_date"]: row for row in replacement_rows}
    missing_existing = [row["trade_date"] for row in existing_rows if row["trade_date"] not in replacement_by_date]
    if missing_existing:
        raise ExplicitRebaseError(
            "replacement baseline does not cover existing dates: " + ",".join(missing_existing[:8])
        )

    tail_count = max(1, int(revised_overlap_must_be_within_last_existing_sessions))
    allowed_revision_dates = {row["trade_date"] for row in existing_rows[-tail_count:]}
    revisions: list[dict[str, Any]] = []
    for old in existing_rows:
        new = replacement_by_date[old["trade_date"]]
        if store.canonical_bytes(old) == store.canonical_bytes(new):
            continue
        if old["trade_date"] not in allowed_revision_dates:
            raise ExplicitRebaseError(
                f"provider revision {old['trade_date']} is outside the authorized settlement tail"
            )
        revisions.append(
            {
                "trade_date": old["trade_date"],
                "changed_fields": _field_diff(old, new),
                "old_canonical_sha256": store.json_sha256(old),
                "new_canonical_sha256": store.json_sha256(new),
            }
        )

    if len(revisions) > int(maximum_revised_overlap_records):
        raise ExplicitRebaseError(
            f"revised overlap count {len(revisions)} exceeds explicit-rebase limit "
            f"{maximum_revised_overlap_records}"
        )
    return revisions


def _build_shards(
    series_id: str,
    records: Sequence[Mapping[str, Any]],
    *,
    record_limit: int,
    byte_limit: int,
) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    rows = [dict(x) for x in records]
    output: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    cursor = 0
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
                raise ExplicitRebaseError("single daily row exceeds shard byte limit")
            break
        name = f"{start:06d}.json"
        doc = store._shard_document(series_id, start, take)
        output.append((name, doc, store._shard_meta(name, start, doc, cursor < len(rows))))
    return output


def _archive_old_series(
    *,
    store_root: Path,
    trade_date: str,
    provider: str,
    symbol: str,
    index: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
    revisions: Sequence[Mapping[str, Any]],
    replacement_record_count: int,
) -> Path:
    path = (
        store_root
        / "recovery"
        / trade_date
        / provider
        / "daily-baseline-rebase"
        / f"{symbol.upper()}.json"
    )
    if path.exists():
        raise ExplicitRebaseError(f"rebase archive already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "market_fact_recovery_archive": True,
        "rebase_recorded_at": datetime.now(timezone.utc).isoformat(),
        "trade_date": trade_date,
        "provider": provider,
        "symbol": symbol.upper(),
        "reason": "provider_settlement_revision_during_daily_baseline_initialization",
        "old_index": dict(index),
        "old_records": [dict(x) for x in records],
        "revisions": [dict(x) for x in revisions],
        "replacement_record_count": replacement_record_count,
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return path


def _replace_series(
    *,
    index_path: Path,
    index: dict[str, Any],
    records: Sequence[Mapping[str, Any]],
    trade_date: str,
    target_records: int,
    archive_path: Path,
    revision_count: int,
    store_root: Path,
) -> None:
    rows = [dict(x) for x in records]
    rows.sort(key=lambda x: x["trade_date"])
    record_limit = int(index.get("shard_record_limit") or store.DEFAULT_RECORD_LIMIT)
    byte_limit = int(index.get("shard_byte_limit") or store.DEFAULT_BYTE_LIMIT)
    series_id = str(index["series_id"])
    shards = _build_shards(
        series_id,
        rows,
        record_limit=record_limit,
        byte_limit=byte_limit,
    )

    sdir = index_path.parent
    for meta in index.get("shards", []) or []:
        old = sdir / str(meta["path"])
        if old.exists():
            old.unlink()
    for name, doc, _meta in shards:
        store._write_json(sdir / name, doc)

    archive_rel = str(archive_path.relative_to(store_root))
    index["record_count"] = len(rows)
    index["first_trade_date"] = rows[0]["trade_date"]
    index["last_trade_date"] = rows[-1]["trade_date"]
    index["shards"] = [meta for _name, _doc, meta in shards]
    index["lineage_segments"] = [
        {
            "ingest_kind": "collector_explicit_daily_baseline_rebase",
            "ingest_trade_date": trade_date,
            "history_outputsize": target_records,
            "start": 0,
            "end": len(rows) - 1,
            "recovery_archive_path": archive_rel,
            "revised_overlap_records": revision_count,
        }
    ]
    index["baseline_initialization"] = {
        "mode": "explicit_audited_provider_settlement_rebase_then_append_only",
        "target_records": target_records,
        "initialized_at": datetime.now(timezone.utc).isoformat(),
        "ingest_trade_date": trade_date,
        "record_count": len(rows),
        "revised_overlap_records": revision_count,
        "recovery_archive_path": archive_rel,
    }
    store._write_json(index_path, index)


def rebase(
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
    series_cfg = config["series"]
    target_records = max(int(x) for x in series_cfg["analysis_windows_may_read_last_sessions"])
    rebase_cfg = series_cfg["baseline_initialization"]["explicit_rebase"]

    key = os.environ.get("TWELVE_DATA_API_KEY")
    if not key:
        return {
            "mode": "daily_baseline_rebase",
            "status": "secret_missing",
            "target_records": target_records,
            "symbols_requested": 0,
            "results": [],
            "failures": [{"reason": "TWELVE_DATA_API_KEY_missing"}],
        }

    provider = collectors.TWELVE_PROVIDER
    budget = config["collector"]["budgets"]["twelve_data_basic"]
    universe = collectors._daily_universe(watchlist, completeness)
    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    for symbol, _asset_class in universe:
        try:
            index_path = _series_index_path(store_root, provider, symbol)
            if not index_path.exists():
                raise ExplicitRebaseError("existing Twelve Data series is missing")
            index = _read_json(index_path)
            expected = {
                "provider": provider,
                "session": "regular",
                "interval": "1d",
                "series_semantics": "daily_regular_ohlcv",
                "adjustment_semantics": "provider_reported",
            }
            for field, value in expected.items():
                if index.get(field) != value:
                    raise ExplicitRebaseError(
                        f"series semantic mismatch {field}: {index.get(field)!r} != {value!r}"
                    )

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
            identity_limited = bool(effective and len(eligible) < target_records)
            if len(eligible) < target_records and not identity_limited:
                raise ExplicitRebaseError(
                    f"provider returned only {len(eligible)} eligible rows for target {target_records}"
                )

            existing = store.read_daily_series(store_root, provider=provider, symbol=symbol)
            revisions = validate_replacement(
                existing,
                eligible,
                maximum_revised_overlap_records=int(rebase_cfg["maximum_revised_overlap_records"]),
                revised_overlap_must_be_within_last_existing_sessions=int(
                    rebase_cfg["revised_overlap_must_be_within_last_existing_sessions"]
                ),
            )
            archive_path = _archive_old_series(
                store_root=store_root,
                trade_date=trade_date,
                provider=provider,
                symbol=symbol,
                index=index,
                records=existing,
                revisions=revisions,
                replacement_record_count=len(eligible),
            )
            _replace_series(
                index_path=index_path,
                index=index,
                records=eligible,
                trade_date=trade_date,
                target_records=target_records,
                archive_path=archive_path,
                revision_count=len(revisions),
                store_root=store_root,
            )
            results.append(
                {
                    "symbol": symbol,
                    "status": "explicit_rebase_complete",
                    "first_trade_date": eligible[0]["trade_date"],
                    "last_trade_date": eligible[-1]["trade_date"],
                    "record_count": len(eligible),
                    "target_records": target_records,
                    "identity_limited": identity_limited,
                    "revised_overlap_records": len(revisions),
                    "revised_trade_dates": [row["trade_date"] for row in revisions],
                    "recovery_archive_path": str(archive_path.relative_to(store_root)),
                }
            )
        except collectors.BudgetExceeded:
            raise
        except Exception as exc:  # noqa: BLE001
            failures.append(
                {
                    "symbol": symbol,
                    "reason": type(exc).__name__,
                    "detail": str(exc)[:480],
                }
            )

    return {
        "mode": "daily_baseline_rebase",
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
    path = store_root / "collector-state" / trade_date[:7] / f"{trade_date}-daily-baseline-rebase.json"
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
    result = rebase(
        trade_date=args.trade_date,
        store_root=store_root,
        watchlist_path=Path(args.watchlist),
        completeness_path=Path(args.data_completeness),
        store_config_path=Path(args.store_config),
        access_path=Path(args.access_config),
    )
    path = _write_result(store_root, args.trade_date, result)
    result["result_path"] = str(path)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    # Successful independent symbols are persisted even if another symbol fails;
    # the result artifact is the explicit retry surface.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
