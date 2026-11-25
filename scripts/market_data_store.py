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
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


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


def assert_legacy_basis(root: str | Path, *, provider: str, symbol: str) -> None:
    """Updated legacy APIs cannot silently consume an explicitly selected basis.

    A catalog's presence is enough to fence, even if malformed or a symlink.
    This cannot constrain older deployed code that does not implement the guard.
    """
    if not provider or "/" in provider or ".." in provider or not symbol or "/" in symbol or ".." in symbol:
        raise MarketDataError("invalid series identity")
    root = Path(root)
    catalog = root / "series-active" / "daily" / provider / (symbol.upper() + ".json")
    if os.path.lexists(catalog) or any(p.is_symlink() for p in (catalog.parent, catalog.parent.parent, catalog.parent.parent.parent)):
        raise MarketDataSourceAffinityError("explicit_versioned_basis_required")


def _series_dir(root: Path, provider: str, symbol: str) -> Path:
    if not provider or "/" in provider or ".." in provider:
        raise MarketDataError("invalid provider")
    if not symbol or "/" in symbol or ".." in symbol:
        raise MarketDataError("invalid symbol")
    assert_legacy_basis(root, provider=provider, symbol=symbol)
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


def repair_active_daily_gaps(root: Path, *, provider: str, symbol: str, asset_class: str,
                             identity_effective_from: str | None, execution_date: str,
                             repairs: list[tuple[dict | None, dict]], verified_gap_sessions: frozenset[str],
                             evidence_path: str,
                             evidence_sha256: str) -> WriteResult:
    """Persist an ordinary gap repair; sealed shard files and old values survive."""
    sdir = _series_dir(root, provider, symbol)
    index_path = sdir / "_index.json"
    before = index_path.read_bytes()
    index = _load_json(index_path)
    expect = dict(series_id=f"{provider}:{symbol}:regular:1d", provider=provider, symbol=symbol,
                  asset_class=asset_class, interval="1d",
                  session="regular", series_semantics="daily_regular_ohlcv",
                  adjustment_semantics="provider_reported", identity_effective_from=identity_effective_from)
    if any(index.get(k) != v for k, v in expect.items()):
        raise MarketDataConflict("gap repair series identity mismatch")
    records = _read_all_records(sdir, index)
    meta = index["shards"][-1]
    start = meta["start"]
    if meta["sealed"] is not False or start != len(records) - meta["count"]:
        raise MarketDataConflict("gap repair requires the active shard")
    source = root / evidence_path
    if (not re.fullmatch(r"[0-9a-f]{64}", evidence_sha256) or evidence_path !=
            f"collector-state/{execution_date[:7]}/price-only-{evidence_sha256}.json"
            or not source.resolve().is_relative_to(root.resolve()) or source.is_symlink()):
        raise MarketDataConflict("gap repair response path invalid")
    payload = source.read_bytes()
    if hashlib.sha256(payload).hexdigest() != evidence_sha256:
        raise MarketDataConflict("gap repair response evidence changed")
    proof = json.loads(payload)
    if (proof.get("kind"), proof.get("symbol"), proof.get("execution_day")) != (
            "normalized_daily_price_only_evidence", symbol, execution_date):
        raise MarketDataConflict("gap repair response identity mismatch")
    inputs = {r["trade_date"]: r for r in proof["records"]}
    if len(inputs) != len(proof["records"]):
        raise MarketDataConflict("gap repair response has duplicate dates")
    by_day = {r["trade_date"]: r for r in records[start:]}
    first, last = min(by_day), max(by_day)
    changes = []
    inserts = []
    prices = {"open", "high", "low", "close"}
    for old, new in repairs:
        day = new["trade_date"]
        fields = {k for k in new if old is None or old.get(k) != new[k]} - {"trade_date"}
        if (not fields or not fields <= prices or by_day.get(day) != old
                or not first <= day <= last or _date(day) >= _date(execution_date)
                or (old is None and (day not in verified_gap_sessions or not first < day < last))
                or (identity_effective_from and day < identity_effective_from)
                or any(new[k] != inputs.get(day, {}).get(k) for k in fields)
                or (old is not None and (any(old.get(k) is not None for k in fields)
                    or set(old) - set(new)))):
            raise MarketDataConflict("gap repair would change a qualified or unproven value")
        by_day[day] = new
        changes.append(dict(trade_date=day, fields=sorted(fields), before=old, after=new))
        if old is None:
            inserts.append(day)
    updated = [by_day[day] for day in sorted(by_day)]
    doc = _shard_document(index["series_id"], start, updated)
    if (len(updated) > index["shard_record_limit"]
            or len(canonical_bytes(doc)) > index["shard_byte_limit"]):
        raise MarketDataConflict("repaired active shard exceeds configured limits")
    evidence = dict(schema_version=1, kind="ordinary_missing_price_repair", symbol=symbol,
                    series_id=index["series_id"], source_path=evidence_path,
                    source_sha256=evidence_sha256, prior_index_blob=git_blob_sha_bytes(before),
                    prior_shard_blob=meta["blob_sha"], prior_shard_path=meta["path"], rows=changes)
    encoded = canonical_bytes(evidence)
    digest = hashlib.sha256(encoded).hexdigest()
    relative = Path("collector-state") / execution_date[:7] / ("ordinary-gap-" + digest + ".json")
    audit = root / relative
    for seg in index["lineage_segments"]:
        if (not isinstance(seg, dict) or type(seg.get("start")) is not int or type(seg.get("end")) is not int
                or not 0 <= seg["start"] <= seg["end"] < len(records)):
            raise MarketDataConflict("gap repair prior lineage is invalid")
        for key in ("start", "end"):
            day = records[seg[key]]["trade_date"]
            seg[key] += sum(insert < day for insert in inserts)
    sorted_days = sorted(by_day)
    for row in changes:
        pos = start + sorted_days.index(row["trade_date"])
        index["lineage_segments"].append(dict(ingest_kind="ordinary_missing_price_repair",
            start=pos, end=pos, normalization_audit_path=relative.as_posix(),
            normalization_audit_sha256=digest, volume_written=False))
    index["record_count"] += len(inserts)
    shard = sdir / meta["path"]
    index["shards"][-1] = _shard_meta(meta["path"], start, doc, False)
    if index_path.read_bytes() != before:
        raise MarketDataConflict("gap repair index moved before write")
    audit.parent.mkdir(parents=True, exist_ok=True)
    with audit.open("xb") as handle:
        handle.write(encoded)
    _write_json(shard, doc)
    _write_json(index_path, index)
    return WriteResult(True, tuple(map(str, (audit, shard, index_path))), index["record_count"])


def quarantine_daily_volume(root: str | Path, *, provider: str, symbol: str,
                            evidence_path: str, evidence_sha256: str,
                            observed_trade_date: str) -> bool:
    """Append only a volume-policy annotation; never alter a market row/shard."""
    root = Path(root)
    sdir = _series_dir(root, provider, symbol)
    index_path = sdir / "_index.json"
    index = _load_json(index_path)
    records = _read_all_records(sdir, index)
    segments = index.get("lineage_segments", [])
    if not records or not isinstance(segments, list) or any(not isinstance(s, dict) for s in segments):
        raise MarketDataConflict("stored lineage schema invalid")
    if any(s.get("volume_policy") == "BLOCKED_UNRESOLVED" or
           s.get("ingest_kind") == "collector_price_only_revision_v1" for s in segments):
        return False
    evidence = root / evidence_path
    if (not evidence.resolve().is_relative_to((root / "collector-state").resolve())
            or evidence.is_symlink()):
        raise MarketDataConflict("volume quarantine evidence invalid")
    raw = evidence.read_bytes()
    proof = json.loads(raw)
    if (hashlib.sha256(raw).hexdigest() != evidence_sha256 or proof.get("symbol") != symbol
            or proof.get("kind") != "normalized_daily_price_only_evidence"
            or proof.get("volume_policy") != "BLOCKED_UNRESOLVED"):
        raise MarketDataConflict("volume quarantine evidence invalid")
    segments.append(dict(ingest_kind="volume_conflict_quarantine_v1",
        start=0, end=len(records)-1, volume_policy="BLOCKED_UNRESOLVED",
        policy_scope="entire_series", observed_trade_date=_date(observed_trade_date),
        normalized_evidence_path=evidence_path, normalized_evidence_sha256=evidence_sha256))
    index["lineage_segments"] = segments
    _write_json(index_path, index)
    return True


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


def _premarket_scope(document: Mapping[str, Any]) -> tuple[set[str], set[str]] | None:
    """Identify a unique same-provider capture universe without trusting counts alone."""
    refs = document.get("data_refs")
    missing = document.get("missing")
    coverage = document.get("coverage")
    if (not isinstance(refs, list) or not refs or not isinstance(missing, list)
            or not isinstance(coverage, dict)):
        return None
    symbols: set[str] = set()
    for ref in refs:
        if (not isinstance(ref, dict) or ref.get("kind") != "premarket_trade_capture"
                or ref.get("provider") != "nasdaq_public_extended"
                or ref.get("window") != "04:00-09:30"):
            return None
        sym = ref.get("symbol")
        if not isinstance(sym, str) or not re.fullmatch(r"[A-Z0-9.-]{1,20}", sym) or sym in symbols:
            return None
        symbols.add(sym)
    if any(not isinstance(sym, str) or not re.fullmatch(r"[A-Z0-9.-]{1,20}", sym) for sym in missing):
        return None
    omitted = set(missing)
    if (len(omitted) != len(missing) or symbols & omitted
            or coverage.get("inherited_ref_count") != 0
            or coverage.get("symbols_requested_in_increment") != len(symbols | omitted)
            or coverage.get("symbols_available_in_increment") != len(symbols)
            or coverage.get("symbols_missing_in_increment") != len(omitted)
            or coverage.get("total_ref_count") != len(symbols)):
        return None
    return symbols, omitted


def _premarket_downgrade(root: Path, directory: Path, latest: Mapping[str, Any],
                         current: Mapping[str, Any]) -> bool:
    window = {"start": "04:00", "end": "09:30"}
    if current.get("stage") != "premarket" or current.get("target_window") != window or not current.get("missing"):
        return False
    sid = latest.get("snapshot_id")
    if not isinstance(sid, str) or not re.fullmatch(r"premarket-[0-9]{6}-et", sid):
        return False
    relative = directory / f"{sid}.json"
    if latest.get("snapshot_path") != str(relative):
        return False
    try:
        old_path = root / relative
        raw = old_path.read_bytes()
        if git_blob_sha_bytes(raw) != latest.get("snapshot_blob_sha"):
            return False
        old = json.loads(raw)
    except (FileNotFoundError, ValueError):
        return False
    if (not isinstance(old, dict) or old.get("schema_version") != SCHEMA_VERSION
            or old.get("snapshot_id") != sid or old.get("stage") != "premarket"
            or old.get("trade_date") != current.get("trade_date")
            or old.get("target_window") != window or old.get("generated_at") != latest.get("generated_at")):
        return False
    old_scope, new_scope = _premarket_scope(old), _premarket_scope(current)
    if old_scope is None or new_scope is None:
        return False
    covered, old_missing = old_scope
    now, missing = new_scope
    return not old_missing and now < covered and now | missing == covered


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
        if _premarket_downgrade(Path(root), rel_dir, latest, doc):
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
