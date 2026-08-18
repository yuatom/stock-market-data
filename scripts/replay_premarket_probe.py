#!/usr/bin/env python3
"""Promote preserved Premarket probe observations into immutable Store facts.

This is a historical data-repair path, not a normal collector.  It never performs
network I/O and never treats a probe artifact itself as a market fact.  It accepts
only a probe whose full-row diagnostics already prove that the original response
was application-successful, target-date-resolvable, parseable and entirely inside
the Premarket session.  The saved first/last trade-row samples are then passed
through the *same* production Nasdaq extended-hours normalizer before a bounded
sample capture is persisted.

Because the legacy shadow probe intentionally stored only first/last trade rows,
this replay does not reconstruct the omitted path and must not claim consolidated
Premarket volume, VWAP, OHLC or continuous-session coverage.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import collection_universe as collection
from market_data_store import git_blob_sha_bytes, write_capture, write_snapshot
from nasdaq_extended_adapter import ExtendedHoursAdapterError, normalize_extended_trade_payload

ET = ZoneInfo("America/New_York")
PROVIDER = "nasdaq_public_extended"
SOURCE_CONTRACT = "nasdaq_public_extended_trade_detail_v1"
FEED_SCOPE = "official_website_delayed_extended_trade_detail_historical_probe_replay_sample"
ENDPOINT_ID = "extended_trading_pre"


class ProbeReplayError(ValueError):
    pass


def _load_json_bytes(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ProbeReplayError("probe document must be an object")
    return value, git_blob_sha_bytes(raw)


def _require_result(result: Mapping[str, Any], *, trade_date: str) -> None:
    if result.get("session_candidate") != "premarket":
        raise ProbeReplayError("probe result is not Premarket")
    if result.get("candidate_id") != ENDPOINT_ID:
        raise ProbeReplayError("probe result endpoint identity mismatch")
    if result.get("http_status") != 200:
        raise ProbeReplayError("probe HTTP status is not 200")
    if result.get("application_status_success") is not True:
        raise ProbeReplayError("probe application status did not succeed")
    if result.get("application_status_code") != 200:
        raise ProbeReplayError("probe application rCode is not 200")
    status = result.get("response_status_object")
    if not isinstance(status, Mapping) or status.get("rCode") != 200:
        raise ProbeReplayError("probe preserved Nasdaq status object is not successful")
    if result.get("error_class") is not None:
        raise ProbeReplayError(f"probe result has error_class={result.get('error_class')!r}")
    if result.get("trade_date_candidate") != trade_date:
        raise ProbeReplayError("probe trade_date does not match requested historical date")
    if result.get("trade_date_resolvable_without_hindsight") is not True:
        raise ProbeReplayError("probe trade date was not independently resolvable")
    if result.get("trade_detail_all_rows_in_candidate_session_window") is not True:
        raise ProbeReplayError("probe did not prove every original row was inside Premarket")
    rows = int(result.get("trade_detail_row_count") or 0)
    parseable = int(result.get("trade_detail_time_parseable_count") or 0)
    matches = int(result.get("trade_detail_session_window_match_count") or 0)
    if rows <= 0 or parseable != rows or matches != rows:
        raise ProbeReplayError("probe full-row time/session diagnostics are incomplete")
    last_update = result.get("last_update_timestamp_et")
    if not isinstance(last_update, str):
        raise ProbeReplayError("probe last-update timestamp is missing")
    parsed = datetime.fromisoformat(last_update)
    if parsed.tzinfo is None or parsed.astimezone(ET).date().isoformat() != trade_date:
        raise ProbeReplayError("probe last-update timestamp is not target-date ET")
    for field in ("trade_detail_first_row", "trade_detail_last_row", "last_update_info"):
        if result.get(field) in (None, [], {}):
            raise ProbeReplayError(f"probe did not preserve required replay sample: {field}")


def _sample_payload(result: Mapping[str, Any]) -> dict[str, Any]:
    first = dict(result["trade_detail_first_row"])
    last = dict(result["trade_detail_last_row"])
    rows = [first]
    if last != first:
        rows.append(last)
    return {
        "status": dict(result["response_status_object"]),
        "data": {
            "lastUpdateInfo": result["last_update_info"],
            "tradeDetailTable": {"rows": rows},
        },
    }


def replay_probe(
    *,
    probe_path: Path,
    trade_date: str,
    store_root: Path,
    universe_config: Mapping[str, Any],
) -> dict[str, Any]:
    probe, probe_blob_sha = _load_json_bytes(probe_path)
    if probe.get("probe") != "nasdaq_extended_hours":
        raise ProbeReplayError("unexpected probe kind")
    if probe.get("market_fact_authority") is not False:
        raise ProbeReplayError("source artifact must remain explicitly non-authoritative")
    if probe.get("automatic_promotion_allowed") is not False:
        raise ProbeReplayError("source artifact must not claim automatic promotion")
    if probe.get("probe_calendar_date_et") != trade_date:
        raise ProbeReplayError("probe calendar date does not match requested trade date")
    if "premarket" not in (probe.get("requested_sessions") or []):
        raise ProbeReplayError("probe did not request Premarket")
    generated_at = str(probe.get("generated_at") or "")
    generated_dt = datetime.fromisoformat(generated_at)
    if generated_dt.tzinfo is None or generated_dt.astimezone(ET).date().isoformat() != trade_date:
        raise ProbeReplayError("probe generated_at is not target-date ET")

    targets = collection.premarket_universe(universe_config)
    expected = {(symbol, asset_class) for symbol, asset_class in targets}
    indexed: dict[tuple[str, str], Mapping[str, Any]] = {}
    for raw in probe.get("results") or []:
        if not isinstance(raw, Mapping) or raw.get("session_candidate") != "premarket":
            continue
        key = (str(raw.get("symbol") or "").upper(), str(raw.get("asset_class") or "").lower())
        if key in indexed:
            raise ProbeReplayError(f"duplicate Premarket probe result for {key}")
        indexed[key] = raw
    if set(indexed) != expected:
        raise ProbeReplayError(
            f"probe target coverage mismatch missing={sorted(expected-set(indexed))} extra={sorted(set(indexed)-expected)}"
        )

    refs: list[dict[str, Any]] = []
    diagnostics: dict[str, Any] = {}
    cutoffs: list[str] = []
    for symbol, asset_class in targets:
        result = indexed[(symbol, asset_class)]
        _require_result(result, trade_date=trade_date)
        normalized = normalize_extended_trade_payload(
            _sample_payload(result),
            session="premarket",
            symbol=symbol,
            asset_class=asset_class,
            endpoint_id=ENDPOINT_ID,
        )
        if normalized.get("trade_date") != trade_date:
            raise ProbeReplayError(f"production normalizer resolved wrong date for {symbol}")
        identity = {
            "symbol": symbol,
            "asset_class": asset_class,
            "ticker_effective_at": collection.ticker_effective_at(universe_config, symbol),
        }
        facts: list[dict[str, Any]] = []
        for candidate in normalized["qualified_candidate_facts"]:
            fact = dict(candidate)
            fact.update(
                {
                    "provider": PROVIDER,
                    "source_contract": SOURCE_CONTRACT,
                    "feed_scope": FEED_SCOPE,
                    "security_identity": identity,
                    "realtime_claim": False,
                    "replay_provenance": {
                        "mode": "historical_probe_replay_sample",
                        "probe_repository": "yuatom/stock-market-data-store",
                        "probe_path": str(probe_path.relative_to(store_root)),
                        "probe_blob_sha": probe_blob_sha,
                        "original_trade_detail_row_count": int(result["trade_detail_row_count"]),
                        "retained_samples_only": True,
                    },
                }
            )
            facts.append(fact)
        cutoff = str(normalized["actual_data_cutoff"])
        cutoffs.append(cutoff)
        capture_id = f"premarket-replay-{symbol.lower()}-{probe_blob_sha[:12]}"
        rel, blob_sha = write_capture(
            store_root,
            trade_date=trade_date,
            session="premarket",
            provider=PROVIDER,
            capture_id=capture_id,
            generated_at=generated_at,
            actual_data_cutoff=cutoff,
            window={"start": "04:00", "end": "09:30"},
            feed_scope=FEED_SCOPE,
            qualified_facts=facts,
            missing_symbols=[],
        )
        refs.append(
            {
                "path": rel,
                "blob_sha": blob_sha,
                "kind": "premarket_historical_probe_replay_sample",
                "window": "04:00-09:30",
                "provider": PROVIDER,
                "symbol": symbol,
                "source_probe_blob_sha": probe_blob_sha,
            }
        )
        diagnostics[symbol] = {
            "sample_fact_count": len(facts),
            "original_trade_detail_row_count": int(result["trade_detail_row_count"]),
            "actual_data_cutoff": cutoff,
        }

    snapshot_id = f"premarket-replay-{probe_blob_sha[:12]}"
    snapshot_path, latest_updated = write_snapshot(
        store_root,
        stage="premarket",
        trade_date=trade_date,
        snapshot_id=snapshot_id,
        generated_at=generated_at,
        data_refs=refs,
        coverage={
            "symbols_requested_in_increment": len(targets),
            "symbols_available_in_increment": len(refs),
            "symbols_missing_in_increment": 0,
            "inherited_ref_count": 0,
            "total_ref_count": len(refs),
            "provider_counts": {PROVIDER: len(refs)},
            "historical_probe_replay_sample": True,
        },
        missing=[],
        target_window={"start": "04:00", "end": "09:30"},
        actual_data_cutoff=max(cutoffs),
    )
    return {
        "mode": "premarket_probe_replay",
        "status": "ok",
        "trade_date": trade_date,
        "probe_path": str(probe_path.relative_to(store_root)),
        "probe_blob_sha": probe_blob_sha,
        "symbols_requested_in_increment": len(targets),
        "symbols_available_in_increment": len(refs),
        "symbols_missing_in_increment": 0,
        "actual_data_cutoff": max(cutoffs),
        "snapshot_path": snapshot_path,
        "snapshot_latest_updated": latest_updated,
        "source_contract": SOURCE_CONTRACT,
        "sample_only": True,
        "diagnostics": diagnostics,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe-path", required=True)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--store-root", default="sources/market-data")
    parser.add_argument("--universe", default="config/collection-universe.json")
    args = parser.parse_args()
    store_root = Path(args.store_root)
    probe_path = Path(args.probe_path)
    if not probe_path.is_absolute():
        probe_path = store_root / probe_path
    universe = collection.load_collection_universe(args.universe)
    result = replay_probe(
        probe_path=probe_path,
        trade_date=args.trade_date,
        store_root=store_root,
        universe_config=universe,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
