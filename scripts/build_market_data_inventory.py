#!/usr/bin/env python3
"""Build a non-authoritative inventory of reusable market-data assets.

The inventory is operational metadata under collector-state.  It summarizes
what the Store already contains without creating market facts or report
authority.  It is regenerated after collector runs so operators and research
runtime can audit coverage without recursively scanning the repository by hand.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _daily_series(store_root: Path) -> list[dict[str, Any]]:
    root = store_root / "series" / "daily"
    rows: list[dict[str, Any]] = []
    if not root.exists():
        return rows
    for index_path in sorted(root.glob("*/*/_index.json")):
        index = _read_json(index_path)
        first_fields: list[str] = []
        shards = index.get("shards") or []
        if shards:
            shard = _read_json(index_path.parent / str(shards[0]["path"]))
            records = shard.get("records") or []
            if records and isinstance(records[0], dict):
                first_fields = sorted(str(k) for k in records[0].keys())
        rows.append(
            {
                "provider": index.get("provider"),
                "symbol": index.get("symbol"),
                "asset_class": index.get("asset_class"),
                "series_semantics": index.get("series_semantics"),
                "adjustment_semantics": index.get("adjustment_semantics"),
                "identity_effective_from": index.get("identity_effective_from"),
                "first_trade_date": index.get("first_trade_date"),
                "last_trade_date": index.get("last_trade_date"),
                "record_count": index.get("record_count"),
                "fields": first_fields,
                "shard_count": len(shards),
                "baseline_initialization": index.get("baseline_initialization"),
                "index_path": str(index_path.relative_to(store_root)),
            }
        )
    return rows


def _stage_snapshots(store_root: Path) -> list[dict[str, Any]]:
    root = store_root / "snapshots"
    rows: list[dict[str, Any]] = []
    if not root.exists():
        return rows
    for latest_path in sorted(root.glob("*/*/*/latest.json")):
        latest = _read_json(latest_path)
        snapshot_rel = latest.get("snapshot_path")
        if not snapshot_rel:
            continue
        snapshot_path = store_root / str(snapshot_rel)
        if not snapshot_path.exists():
            rows.append(
                {
                    "latest_path": str(latest_path.relative_to(store_root)),
                    "snapshot_path": snapshot_rel,
                    "status": "missing_snapshot_target",
                }
            )
            continue
        snapshot = _read_json(snapshot_path)
        refs = snapshot.get("data_refs") or []
        providers = sorted({str(ref.get("provider")) for ref in refs if ref.get("provider")})
        windows = sorted({str(ref.get("window")) for ref in refs if ref.get("window")})
        rows.append(
            {
                "trade_date": snapshot.get("trade_date"),
                "stage": snapshot.get("stage"),
                "snapshot_id": snapshot.get("snapshot_id"),
                "generated_at": snapshot.get("generated_at"),
                "actual_data_cutoff": snapshot.get("actual_data_cutoff"),
                "ref_count": len(refs),
                "missing_count": len(snapshot.get("missing") or []),
                "providers": providers,
                "windows": windows,
                "snapshot_path": snapshot_rel,
            }
        )
    return rows


def _session_capture_summary(store_root: Path) -> list[dict[str, Any]]:
    root = store_root / "sessions"
    groups: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    if not root.exists():
        return []
    for path in sorted(root.glob("*/*/*/*/*/*.json")):
        try:
            doc = _read_json(path)
        except Exception:  # noqa: BLE001
            continue
        key = (
            str(doc.get("trade_date") or ""),
            str(doc.get("session") or ""),
            str((doc.get("window") or {}).get("start") or "full"),
            str(doc.get("provider") or ""),
        )
        row = groups.setdefault(
            key,
            {
                "trade_date": doc.get("trade_date"),
                "session": doc.get("session"),
                "window_start": (doc.get("window") or {}).get("start"),
                "window_end": (doc.get("window") or {}).get("end"),
                "provider": doc.get("provider"),
                "capture_count": 0,
                "qualified_fact_count": 0,
                "symbols": set(),
                "min_source_timestamp": None,
                "max_source_timestamp": None,
            },
        )
        row["capture_count"] += 1
        facts = doc.get("qualified_facts") or []
        row["qualified_fact_count"] += len(facts)
        for fact in facts:
            if not isinstance(fact, dict):
                continue
            symbol = fact.get("symbol")
            if symbol:
                row["symbols"].add(str(symbol))
            ts = fact.get("source_timestamp") or fact.get("event_time")
            if ts:
                ts = str(ts)
                if row["min_source_timestamp"] is None or ts < row["min_source_timestamp"]:
                    row["min_source_timestamp"] = ts
                if row["max_source_timestamp"] is None or ts > row["max_source_timestamp"]:
                    row["max_source_timestamp"] = ts
    out: list[dict[str, Any]] = []
    for row in groups.values():
        item = dict(row)
        item["symbols"] = sorted(row["symbols"])
        item["symbol_count"] = len(item["symbols"])
        out.append(item)
    return sorted(
        out,
        key=lambda x: (
            str(x.get("trade_date") or ""),
            str(x.get("session") or ""),
            str(x.get("window_start") or ""),
            str(x.get("provider") or ""),
        ),
    )


def _probe_summary(store_root: Path) -> dict[str, Any]:
    root = store_root / "collector-state" / "probes" / "nasdaq-extended"
    files = sorted(root.glob("*/*/*.json")) if root.exists() else []
    trade_dates = sorted({path.parent.name for path in files})
    return {
        "provider_probe": "nasdaq_extended",
        "market_fact_authority": False,
        "file_count": len(files),
        "distinct_trade_dates": len(trade_dates),
        "trade_dates": trade_dates,
        "first_trade_date": trade_dates[0] if trade_dates else None,
        "last_trade_date": trade_dates[-1] if trade_dates else None,
    }


def build_inventory(store_root: Path) -> dict[str, Any]:
    series = _daily_series(store_root)
    snapshots = _stage_snapshots(store_root)
    sessions = _session_capture_summary(store_root)
    provider_counts: dict[str, int] = {}
    for row in series:
        provider = str(row.get("provider") or "unknown")
        provider_counts[provider] = provider_counts.get(provider, 0) + 1
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "market_fact_authority": False,
        "purpose": "derived operational inventory of persisted market-data assets",
        "daily_series_summary": {
            "series_count": len(series),
            "provider_series_counts": dict(sorted(provider_counts.items())),
            "series": series,
        },
        "session_capture_summary": sessions,
        "stage_snapshot_summary": snapshots,
        "extended_hours_probe_summary": _probe_summary(store_root),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--store-root", default="sources/market-data")
    parser.add_argument(
        "--output",
        default="sources/market-data/collector-state/market-data-inventory.json",
    )
    args = parser.parse_args(argv)
    store_root = Path(args.store_root)
    output = Path(args.output)
    inventory = build_inventory(store_root)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(inventory, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(inventory, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
