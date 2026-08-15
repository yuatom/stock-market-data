#!/usr/bin/env python3
"""Reusable market-data persistence primitives.

This module deliberately owns no provider selection and no report publication.
It persists already-qualified facts with provider/session/identity lineage.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

SCHEMA_VERSION = 1
DEFAULT_RECORD_LIMIT = 128
DEFAULT_BYTE_LIMIT = 24576
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
HEX40_RE = re.compile(r"^[0-9a-f]{40}$")


class MarketDataError(RuntimeError):
    pass


class MarketDataConflict(MarketDataError):
    pass


class MarketDataIdentityError(MarketDataError):
    pass


class MarketDataSourceAffinityError(MarketDataError):
    pass


@dataclass(frozen=True)
class WriteResult:
    changed: bool
    paths: tuple[str, ...]
    record_count: int | None = None


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def json_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def git_blob_sha_bytes(data: bytes) -> str:
    header = f"blob {len(data)}\0".encode("ascii")
    return hashlib.sha1(header + data).hexdigest()


def git_blob_sha(value: Any) -> str:
    return git_blob_sha_bytes(canonical_bytes(value))


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_bytes(value) + b"\n"
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("wb") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _date(value: str) -> str:
    if not isinstance(value, str) or not DATE_RE.match(value):
        raise MarketDataError(f"invalid trade_date: {value!r}")
    datetime.strptime(value, "%Y-%m-%d")
    return value


def _datetime(value: str) -> datetime:
    if not isinstance(value, str):
        raise MarketDataError("timestamp must be a string")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        raise MarketDataError(f"timestamp must include timezone: {value}")
    return parsed


def _normalize_bar(bar: Mapping[str, Any]) -> dict[str, Any]:
    if "trade_date" not in bar or "close" not in bar:
        raise MarketDataError("daily bar requires trade_date and close")
    normalized: dict[str, Any] = {"trade_date": _date(str(bar["trade_date"]))}
    for field in ("open", "high", "low", "close", "volume", "source_timestamp"):
        if field in bar and bar[field] is not None:
            normalized[field] = bar[field]
    if not isinstance(normalized["close"], (int, float)) or isinstance(normalized["close"], bool):
        raise MarketDataError("close must be numeric")
    if "volume" in normalized and (not isinstance(normalized["volume"], (int, float)) or normalized["volume"] < 0):
        raise MarketDataError("volume must be non-negative numeric")
    return normalized


def _series_dir(root: Path, provider: str, symbol: str) -> Path:
    if not provider or "/" in provider or ".." in provider:
        raise MarketDataError("invalid provider")
    if not symbol or "/" in symbol or ".." in symbol:
        raise MarketDataError("invalid symbol")
    return root / "series" / "daily" / provider / symbol.upper()


def _shard_document(series_id: str, start: int, records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "series_id": series_id, "start": start, "records": list(records)}


def _shard_meta(path_name: str, start: int, doc: Mapping[str, Any], sealed: bool) -> dict[str, Any]:
    records = doc["records"]
    raw = canonical_bytes(doc)
    return {
        "path": path_name,
        "start": start,
        "end": start + len(records) - 1,
        "count": len(records),
        "sealed": bool(sealed),
        "byte_length": len(raw),
        "json_sha256": hashlib.sha256(raw).hexdigest(),
        "blob_sha": git_blob_sha_bytes(raw + b"\n"),
    }


def _read_all_records(series_dir: Path, index: Mapping[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for meta in index.get("shards", []):
        doc = _load_json(series_dir / meta["path"])
        if doc.get("series_id") != index.get("series_id"):
            raise MarketDataConflict(f"series id mismatch in {meta['path']}")
        actual = _shard_meta(meta["path"], int(meta["start"]), doc, bool(meta["sealed"]))
        for field in ("start", "end", "count", "byte_length", "json_sha256", "blob_sha"):
            if actual[field] != meta[field]:
                raise MarketDataConflict(f"shard integrity mismatch {meta['path']} field={field}")
        records.extend(doc["records"])
    if len(records) != index.get("record_count", 0):
        raise MarketDataConflict("index record_count mismatch")
    dates = [r["trade_date"] for r in records]
    if dates != sorted(dates) or len(dates) != len(set(dates)):
        raise MarketDataConflict("series dates are not unique ascending")
    return records


def append_daily_bars(
    root: str | Path,
    *,
    provider: str,
    symbol: str,
    asset_class: str,
    bars: Iterable[Mapping[str, Any]],
    series_semantics: str = "daily_regular_close",
    adjustment_semantics: str = "provider_reported",
    session: str = "regular",
    identity_effective_from: str | None = None,
    lineage: Mapping[str, Any] | None = None,
    shard_record_limit: int = DEFAULT_RECORD_LIMIT,
    shard_byte_limit: int = DEFAULT_BYTE_LIMIT,
) -> WriteResult:
    if session != "regular":
        raise MarketDataSourceAffinityError("daily series currently requires regular session")
    effective = _date(identity_effective_from) if identity_effective_from else None
    incoming = [_normalize_bar(b) for b in bars]
    if not incoming:
        return WriteResult(False, tuple(), None)
    incoming.sort(key=lambda x: x["trade_date"])
    if len({b["trade_date"] for b in incoming}) != len(incoming):
        raise MarketDataConflict("duplicate trade_date inside append batch")
    if effective and any(b["trade_date"] < effective for b in incoming):
        bad = min(b["trade_date"] for b in incoming if b["trade_date"] < effective)
        raise MarketDataIdentityError(f"{symbol} bar {bad} precedes identity effective date {effective}")

    root_path = Path(root)
    sdir = _series_dir(root_path, provider, symbol)
    index_path = sdir / "_index.json"
    series_id = f"{provider}:{symbol.upper()}:regular:1d"
    if index_path.exists():
        index = _load_json(index_path)
        expected = {
            "series_id": series_id,
            "provider": provider,
            "symbol": symbol.upper(),
            "asset_class": asset_class,
            "session": session,
            "interval": "1d",
            "series_semantics": series_semantics,
            "adjustment_semantics": adjustment_semantics,
            "identity_effective_from": effective,
        }
        for field, value in expected.items():
            if index.get(field) != value:
                raise MarketDataSourceAffinityError(f"series affinity mismatch {field}: {index.get(field)!r} != {value!r}")
        existing = _read_all_records(sdir, index)
    else:
        index = {
            "schema_version": SCHEMA_VERSION,
            "series_id": series_id,
            "provider": provider,
            "symbol": symbol.upper(),
            "asset_class": asset_class,
            "session": session,
            "interval": "1d",
            "series_semantics": series_semantics,
            "adjustment_semantics": adjustment_semantics,
            "identity_effective_from": effective,
            "sort_order": "trade_date_asc",
            "record_count": 0,
            "first_trade_date": None,
            "last_trade_date": None,
            "shard_record_limit": shard_record_limit,
            "shard_byte_limit": shard_byte_limit,
            "shards": [],
            "lineage_segments": [],
        }
        existing = []

    by_date = {r["trade_date"]: r for r in existing}
    new_records: list[dict[str, Any]] = []
    for bar in incoming:
        old = by_date.get(bar["trade_date"])
        if old is not None:
            if canonical_bytes(old) != canonical_bytes(bar):
                raise MarketDataConflict(f"conflicting bar for {symbol} {bar['trade_date']}")
            continue
        if existing and bar["trade_date"] <= existing[-1]["trade_date"]:
            raise MarketDataConflict(f"out-of-order append {bar['trade_date']} <= {existing[-1]['trade_date']}")
        if new_records and bar["trade_date"] <= new_records[-1]["trade_date"]:
            raise MarketDataConflict("append batch must advance trade_date")
        new_records.append(bar)

    if not new_records:
        return WriteResult(False, tuple(), len(existing))

    changed_paths: list[str] = []
    shards = list(index["shards"])
    pending = list(new_records)
    if shards and not shards[-1]["sealed"]:
        active_meta = dict(shards[-1])
        active_path = sdir / active_meta["path"]
        active_doc = _load_json(active_path)
        active_records = list(active_doc["records"])
        while pending:
            candidate_records = active_records + [pending[0]]
            candidate_doc = _shard_document(series_id, active_meta["start"], candidate_records)
            if len(candidate_records) <= shard_record_limit and len(canonical_bytes(candidate_doc)) <= shard_byte_limit:
                active_records.append(pending.pop(0))
            else:
                break
        active_doc = _shard_document(series_id, active_meta["start"], active_records)
        sealed = bool(pending)
        _write_json(active_path, active_doc)
        changed_paths.append(str(active_path))
        shards[-1] = _shard_meta(active_meta["path"], active_meta["start"], active_doc, sealed)

    while pending:
        start = sum(int(x["count"]) for x in shards)
        take: list[dict[str, Any]] = []
        while pending:
            candidate = take + [pending[0]]
            doc = _shard_document(series_id, start, candidate)
            if len(candidate) <= shard_record_limit and len(canonical_bytes(doc)) <= shard_byte_limit:
                take.append(pending.pop(0))
            else:
                if not take:
                    raise MarketDataError("single daily bar exceeds shard byte limit")
                break
        sealed = bool(pending)
        name = f"{start:06d}.json"
        doc = _shard_document(series_id, start, take)
        _write_json(sdir / name, doc)
        changed_paths.append(str(sdir / name))
        shards.append(_shard_meta(name, start, doc, sealed))

    all_records = existing + new_records
    index["record_count"] = len(all_records)
    index["first_trade_date"] = all_records[0]["trade_date"]
    index["last_trade_date"] = all_records[-1]["trade_date"]
    index["shards"] = shards
    if lineage:
        segment = dict(lineage)
        segment.update({"start": len(existing), "end": len(all_records) - 1})
        index.setdefault("lineage_segments", []).append(segment)
    _write_json(index_path, index)
    changed_paths.append(str(index_path))
    return WriteResult(True, tuple(changed_paths), len(all_records))


def read_daily_series(root: str | Path, *, provider: str, symbol: str, last_sessions: int | None = None) -> list[dict[str, Any]]:
    sdir = _series_dir(Path(root), provider, symbol)
    index = _load_json(sdir / "_index.json")
    records = _read_all_records(sdir, index)
    if last_sessions is not None:
        if last_sessions <= 0:
            return []
        return records[-last_sessions:]
    return records


def read_daily_exact(root: str | Path, *, provider: str, symbol: str, trade_date: str) -> dict[str, Any] | None:
    target = _date(trade_date)
    for record in reversed(read_daily_series(root, provider=provider, symbol=symbol)):
        if record["trade_date"] == target:
            return record
        if record["trade_date"] < target:
            break
    return None


def write_capture(
    root: str | Path,
    *,
    trade_date: str,
    session: str,
    provider: str,
    capture_id: str,
    generated_at: str,
    qualified_facts: Sequence[Mapping[str, Any]],
    window: Mapping[str, Any] | None = None,
    actual_data_cutoff: str | None = None,
    feed_scope: str | None = None,
    missing_symbols: Sequence[str] = (),
) -> tuple[str, str]:
    _date(trade_date)
    _datetime(generated_at)
    if actual_data_cutoff:
        _datetime(actual_data_cutoff)
    doc = {
        "schema_version": SCHEMA_VERSION,
        "capture_id": capture_id,
        "trade_date": trade_date,
        "session": session,
        "provider": provider,
        "generated_at": generated_at,
        "actual_data_cutoff": actual_data_cutoff,
        "window": dict(window) if window else None,
        "feed_scope": feed_scope,
        "qualified_facts": [dict(x) for x in qualified_facts],
        "missing_symbols": list(missing_symbols),
    }
    month = trade_date[:7]
    window_name = "full" if not window else f"{window.get('start','na')}-{window.get('end','na')}".replace(":", "")
    rel = Path("sessions") / month / trade_date / session / window_name / provider / f"{capture_id}.json"
    path = Path(root) / rel
    if path.exists():
        if canonical_bytes(_load_json(path)) != canonical_bytes(doc):
            raise MarketDataConflict(f"immutable capture conflict: {rel}")
        return str(rel), git_blob_sha_bytes(canonical_bytes(doc) + b"\n")
    _write_json(path, doc)
    return str(rel), git_blob_sha_bytes(canonical_bytes(doc) + b"\n")


def write_snapshot(
    root: str | Path,
    *,
    stage: str,
    trade_date: str,
    snapshot_id: str,
    generated_at: str,
    data_refs: Sequence[Mapping[str, Any]],
    coverage: Mapping[str, Any],
    missing: Sequence[Any],
    target_window: Mapping[str, Any] | None = None,
    actual_data_cutoff: str | None = None,
) -> tuple[str, bool]:
    _date(trade_date)
    current_dt = _datetime(generated_at)
    for ref in data_refs:
        if not ref.get("path") or not HEX40_RE.match(str(ref.get("blob_sha", ""))):
            raise MarketDataError("snapshot data ref requires path and 40-char blob_sha")
    doc = {
        "schema_version": SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "stage": stage,
        "trade_date": trade_date,
        "target_window": dict(target_window) if target_window else None,
        "generated_at": generated_at,
        "actual_data_cutoff": actual_data_cutoff,
        "coverage": dict(coverage),
        "data_refs": [dict(x) for x in data_refs],
        "missing": list(missing),
    }
    rel_dir = Path("snapshots") / trade_date[:7] / trade_date / stage
    rel = rel_dir / f"{snapshot_id}.json"
    path = Path(root) / rel
    if path.exists():
        if canonical_bytes(_load_json(path)) != canonical_bytes(doc):
            raise MarketDataConflict(f"immutable snapshot conflict: {rel}")
    else:
        _write_json(path, doc)
    latest_path = Path(root) / rel_dir / "latest.json"
    if latest_path.exists():
        latest = _load_json(latest_path)
        latest_dt = _datetime(latest["generated_at"])
        if current_dt < latest_dt:
            return str(rel), False
        if current_dt == latest_dt and latest.get("snapshot_id") != snapshot_id:
            raise MarketDataConflict("two snapshots share generated_at but differ in id")
        if current_dt == latest_dt and latest.get("snapshot_id") == snapshot_id:
            return str(rel), False
    snapshot_blob = git_blob_sha_bytes(canonical_bytes(doc) + b"\n")
    latest_doc = {
        "schema_version": SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "snapshot_path": str(rel),
        "snapshot_blob_sha": snapshot_blob,
        "generated_at": generated_at,
    }
    _write_json(latest_path, latest_doc)
    return str(rel), True


def _cli() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("read-daily")
    p.add_argument("--root", default="sources/market-data")
    p.add_argument("--provider", required=True)
    p.add_argument("--symbol", required=True)
    p.add_argument("--last", type=int)
    args = parser.parse_args()
    if args.command == "read-daily":
        print(json.dumps(read_daily_series(args.root, provider=args.provider, symbol=args.symbol, last_sessions=args.last), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
