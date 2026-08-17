#!/usr/bin/env python3
"""Collect request-scoped market facts for opportunity-discovery candidates.

This deliberately does not mutate config/collection-universe.json. Candidate
membership is bounded to one signed request and is never a research-priority or
opportunity-qualification authority.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import yaml

import market_data_collectors as collectors
import market_data_store as store
from market_data_collection import collect_regular_window


class DynamicCandidateCollectionError(RuntimeError):
    pass


HEX40 = re.compile(r"^[0-9a-f]{40}$")
SYMBOL = re.compile(r"^[A-Z][A-Z0-9.-]{0,9}$")
ALLOWED_STAGES = {"open_30m", "close"}


def _load_yaml(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise DynamicCandidateCollectionError(f"{path} must contain a YAML mapping")
    return value


def _load_request(path: Path, *, expected_contract_sha: str | None = None) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise DynamicCandidateCollectionError("request must be a JSON object")
    required = {
        "schema_version", "request_id", "requested_at", "trade_date", "stage",
        "research_repository", "research_repository_commit_sha", "market_data_contract_sha",
        "candidate_symbols",
    }
    missing = sorted(required - set(value))
    if missing:
        raise DynamicCandidateCollectionError(f"request missing fields: {missing}")
    allowed = required | {"transaction_id"}
    extra = sorted(set(value) - allowed)
    if extra:
        raise DynamicCandidateCollectionError(f"request has unsupported fields: {extra}")
    if value.get("schema_version") != 1:
        raise DynamicCandidateCollectionError("schema_version must be 1")
    if value.get("research_repository") != "yuatom/stock-dairy":
        raise DynamicCandidateCollectionError("research_repository is unauthorized")
    if value.get("stage") not in ALLOWED_STAGES:
        raise DynamicCandidateCollectionError("stage must be open_30m or close")
    for field in ("research_repository_commit_sha", "market_data_contract_sha"):
        if not HEX40.fullmatch(str(value.get(field) or "")):
            raise DynamicCandidateCollectionError(f"{field} must be a 40-char SHA")
    if expected_contract_sha and value.get("market_data_contract_sha") != expected_contract_sha:
        raise DynamicCandidateCollectionError("request market_data_contract_sha does not match collector contract")
    symbols = value.get("candidate_symbols")
    if not isinstance(symbols, list) or not (1 <= len(symbols) <= 8):
        raise DynamicCandidateCollectionError("candidate_symbols must contain 1-8 symbols")
    normalized = [str(symbol).upper() for symbol in symbols]
    if len(normalized) != len(set(normalized)) or any(not SYMBOL.fullmatch(symbol) for symbol in normalized):
        raise DynamicCandidateCollectionError("candidate_symbols must be unique valid uppercase tickers")
    value["candidate_symbols"] = normalized
    try:
        datetime.fromisoformat(str(value["requested_at"]).replace("Z", "+00:00"))
        datetime.fromisoformat(str(value["trade_date"]))
    except ValueError as exc:
        raise DynamicCandidateCollectionError("requested_at or trade_date is invalid") from exc
    return value


def _ensure_daily_history(
    *,
    symbols: Sequence[str],
    trade_date: str,
    store_root: Path,
    config: dict[str, Any],
    access: dict[str, Any],
    target_records: int,
) -> tuple[list[str], list[dict[str, str]]]:
    key = os.environ.get(str((access.get("twelve_data_basic") or {}).get("api_key_env") or "TWELVE_DATA_API_KEY"))
    if not key:
        return [], [{"symbol": symbol, "reason": "TWELVE_DATA_API_KEY_missing"} for symbol in symbols]
    budget = config["collector"]["budgets"]["twelve_data_basic"]
    available: list[str] = []
    failures: list[dict[str, str]] = []
    for symbol in symbols:
        try:
            collectors.wait_for_twelve_budget(
                store_root,
                trade_date,
                per_day=int(budget["hard_credits_per_day"]),
                per_minute=int(budget["hard_credits_per_minute"]),
            )
            fetched = collectors.fetch_twelve_daily(symbol, key, access, outputsize=target_records)
            collectors.consume_twelve_credit(
                store_root,
                trade_date,
                amount=1,
                per_day=int(budget["hard_credits_per_day"]),
                per_minute=int(budget["hard_credits_per_minute"]),
            )
            eligible = [row for row in fetched if str(row.get("trade_date") or "") < trade_date]
            if not eligible:
                failures.append({"symbol": symbol, "reason": "no_cutoff_valid_daily_history"})
                continue
            store.append_daily_bars(
                store_root,
                provider=collectors.TWELVE_PROVIDER,
                symbol=symbol,
                asset_class="stocks",
                bars=eligible,
                identity_effective_from=None,
                series_semantics="daily_regular_ohlcv",
                adjustment_semantics="provider_reported",
                lineage={
                    "ingest_kind": "dynamic_opportunity_candidate_history",
                    "ingest_trade_date": trade_date,
                    "history_outputsize": target_records,
                },
            )
            available.append(symbol)
        except Exception as exc:  # noqa: BLE001
            failures.append({"symbol": symbol, "reason": f"{type(exc).__name__}:{str(exc)[:240]}"})
    return available, failures


def collect_request(
    *,
    request: dict[str, Any],
    store_root: Path,
    store_config_path: Path,
    access_path: Path,
    dynamic_config_path: Path,
) -> dict[str, Any]:
    config = collectors.load_yaml(store_config_path)
    access = collectors.load_yaml(access_path)
    dynamic = _load_yaml(dynamic_config_path)
    symbols = list(request["candidate_symbols"])
    stage = str(request["stage"])
    trade_date = str(request["trade_date"])
    target_records = int((((dynamic.get("collection") or {}).get("daily_series") or {}).get("target_history_sessions") or 256))

    daily_available, daily_failures = _ensure_daily_history(
        symbols=symbols,
        trade_date=trade_date,
        store_root=store_root,
        config=config,
        access=access,
        target_records=target_records,
    )

    if stage == "open_30m":
        start_et, end_et = "09:30", "10:00"
    else:
        start_et, end_et = "15:45", "16:00"

    result = collect_regular_window(
        mode=f"discovery_{stage}",
        stage=f"discovery_{stage}",
        trade_date=trade_date,
        start_et=start_et,
        end_et=end_et,
        store_root=store_root,
        universe_config={"context_proxies": {}, "sector_etfs": []},
        config=config,
        access=access,
        eligible_universe=[(symbol, "stocks") for symbol in symbols],
    )
    result.update(
        {
            "request_id": request["request_id"],
            "research_repository_commit_sha": request["research_repository_commit_sha"],
            "market_data_contract_sha": request["market_data_contract_sha"],
            "candidate_symbols": symbols,
            "daily_history_available": sorted(daily_available),
            "daily_history_failures": daily_failures,
            "snapshot_stage": f"discovery_{stage}",
        }
    )
    state = store_root / "collector-state" / trade_date[:7] / f"{trade_date}-dynamic-candidate-{stage}.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state_payload = dict(result)
    state_payload["schema_version"] = 1
    state_payload["market_fact_authority"] = False
    state_payload["recorded_at"] = datetime.now(timezone.utc).isoformat()
    state.write_text(json.dumps(state_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    result["result_state_path"] = str(state.relative_to(store_root))
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    parser.add_argument("--store-root", default="sources/market-data")
    parser.add_argument("--store-config", default="config/market-data-store.yaml")
    parser.add_argument("--access-config", default="config/market-data-collector-access.yaml")
    parser.add_argument("--dynamic-config", default="config/dynamic-candidate-collection.yaml")
    parser.add_argument("--expected-contract-sha")
    args = parser.parse_args(argv)
    request = _load_request(Path(args.request), expected_contract_sha=args.expected_contract_sha)
    result = collect_request(
        request=request,
        store_root=Path(args.store_root),
        store_config_path=Path(args.store_config),
        access_path=Path(args.access_config),
        dynamic_config_path=Path(args.dynamic_config),
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
