#!/usr/bin/env python3
"""Probe Nasdaq public extended-hours JSON surfaces without creating market facts.

Outputs are diagnostics under collector-state/probes only. HTTP 200 alone is not
success: Nasdaq's JSON status.rCode must also report success. Promotion remains a
separate explicit contract change after repeated live-session observations.
"""
from __future__ import annotations

import argparse
import json
import re
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import yaml

ET = ZoneInfo("America/New_York")


def _load_yaml(path: str | Path) -> dict[str, Any]:
    value = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected mapping: {path}")
    return value


def _rows(value: Any) -> list[Any]:
    if isinstance(value, dict) and isinstance(value.get("rows"), list):
        return list(value["rows"])
    return []


def _keys(value: Any) -> list[str]:
    if isinstance(value, dict):
        return sorted(str(key) for key in value)
    return []


def _safe_sample(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _safe_sample(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_safe_sample(item) for item in value[:8]]
    if isinstance(value, str):
        return value[:1000]
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return str(value)[:1000]


def _parse_trade_clock(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().upper()
    for fmt in ("%H:%M:%S", "%I:%M:%S %p", "%I:%M %p"):
        try:
            return datetime.strptime(text, fmt).strftime("%H:%M:%S")
        except ValueError:
            continue
    return None


def _parse_last_update_timestamp(value: Any) -> datetime | None:
    values = value if isinstance(value, list) else [value]
    for item in values:
        if not isinstance(item, str):
            continue
        match = re.search(r"Data last updated\s+(.+?)\s+ET\.?$", item.strip(), re.IGNORECASE)
        if not match:
            continue
        text = match.group(1).strip()
        for fmt in ("%b %d, %Y %I:%M %p", "%B %d, %Y %I:%M %p"):
            try:
                return datetime.strptime(text, fmt).replace(tzinfo=ET)
            except ValueError:
                continue
    return None


def _session_clock_match(clock: str, session: str) -> bool:
    if session == "premarket":
        return "04:00:00" <= clock < "09:30:00"
    if session == "after_hours":
        return "16:00:00" <= clock < "20:00:00"
    return False


def _probe_targets(spec: Mapping[str, Any]) -> list[dict[str, str]]:
    raw_targets = spec.get("probe_targets") or []
    targets: list[dict[str, str]] = []
    for raw in raw_targets:
        if not isinstance(raw, Mapping):
            raise ValueError("nasdaq_extended_hours_probe.probe_targets entries must be mappings")
        symbol = str(raw.get("symbol") or "").strip().upper()
        asset_class = str(raw.get("asset_class") or "").strip().lower()
        if not symbol or asset_class not in {"stocks", "etf"}:
            raise ValueError(f"invalid Nasdaq extended-hours probe target: {raw!r}")
        targets.append({"symbol": symbol, "asset_class": asset_class})
    if not targets:
        raise ValueError("nasdaq_extended_hours_probe.probe_targets must not be empty")
    identities = [(target["symbol"], target["asset_class"]) for target in targets]
    if len(identities) != len(set(identities)):
        raise ValueError("duplicate Nasdaq extended-hours probe target")
    return targets


def _probe_url(
    url: str,
    *,
    session_candidate: str,
    timeout: int,
    user_agent: str,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "url": url,
        "http_status": None,
        "content_type": None,
        "response_length": 0,
        "json_top_level_keys": [],
        "response_status_object": None,
        "application_status_code": None,
        "application_status_success": False,
        "application_error_messages": None,
        "data_top_level_keys": [],
        "session_text": None,
        "last_update_info": None,
        "last_update_timestamp_et": None,
        "filter_list": None,
        "info_table_row_count": 0,
        "info_table_first_row_keys": [],
        "info_table_first_row": None,
        "trade_detail_row_count": 0,
        "trade_detail_first_row_keys": [],
        "trade_detail_first_row": None,
        "trade_detail_last_row": None,
        "trade_detail_time_parseable_count": 0,
        "trade_detail_time_unparseable_count": 0,
        "trade_detail_earliest_time_et": None,
        "trade_detail_latest_time_et": None,
        "trade_detail_session_window_match_count": 0,
        "trade_detail_all_rows_in_candidate_session_window": False,
        "trade_date_candidate": None,
        "trade_date_resolution_basis": None,
        "trade_date_resolvable_without_hindsight": False,
        "latest_trade_lag_seconds_vs_last_update": None,
        "previous_info": None,
        "error_class": None,
        "error_detail": None,
    }
    request = urllib.request.Request(
        url,
        headers={"User-Agent": user_agent, "Accept": "application/json,text/plain,*/*"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            result["http_status"] = int(response.status)
            result["content_type"] = response.headers.get("content-type", "")
    except urllib.error.HTTPError as exc:
        raw = exc.read() if hasattr(exc, "read") else b""
        result["http_status"] = int(exc.code)
        result["content_type"] = exc.headers.get("content-type", "") if exc.headers else ""
        result["error_class"] = "http_error"
        result["error_detail"] = str(exc)[:500]
    except Exception as exc:
        result["error_class"] = type(exc).__name__
        result["error_detail"] = str(exc)[:500]
        return result

    result["response_length"] = len(raw)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        if result["error_class"] is None:
            result["error_class"] = "non_json_response"
            result["error_detail"] = str(exc)[:500]
        return result
    if not isinstance(payload, dict):
        result["error_class"] = result["error_class"] or "json_not_object"
        return result

    result["json_top_level_keys"] = _keys(payload)
    status = payload.get("status")
    result["response_status_object"] = _safe_sample(status)
    if isinstance(status, dict):
        raw_code = status.get("rCode")
        try:
            result["application_status_code"] = int(raw_code) if raw_code is not None else None
        except (TypeError, ValueError):
            result["application_status_code"] = None
        result["application_error_messages"] = _safe_sample(status.get("bCodeMessage"))
    result["application_status_success"] = (
        result["http_status"] == 200 and result["application_status_code"] == 200
    )
    if not result["application_status_success"] and result["error_class"] is None:
        result["error_class"] = "application_status_error"
        result["error_detail"] = json.dumps(
            result["application_error_messages"], ensure_ascii=False, sort_keys=True
        )[:500]

    data = payload.get("data")
    if not isinstance(data, dict):
        return result
    result["data_top_level_keys"] = _keys(data)
    result["session_text"] = _safe_sample(data.get("sessionText"))
    result["last_update_info"] = _safe_sample(data.get("lastUpdateInfo"))
    last_update = _parse_last_update_timestamp(data.get("lastUpdateInfo"))
    if last_update is not None:
        result["last_update_timestamp_et"] = last_update.isoformat()
    result["filter_list"] = _safe_sample(data.get("filterList"))

    info_rows = _rows(data.get("infoTable"))
    result["info_table_row_count"] = len(info_rows)
    if info_rows:
        result["info_table_first_row_keys"] = _keys(info_rows[0])
        result["info_table_first_row"] = _safe_sample(info_rows[0])

    trade_rows = _rows(data.get("tradeDetailTable"))
    result["trade_detail_row_count"] = len(trade_rows)
    if trade_rows:
        result["trade_detail_first_row_keys"] = _keys(trade_rows[0])
        result["trade_detail_first_row"] = _safe_sample(trade_rows[0])
        result["trade_detail_last_row"] = _safe_sample(trade_rows[-1])

    parsed_clocks: list[str] = []
    for row in trade_rows:
        clock = _parse_trade_clock(row.get("time") if isinstance(row, dict) else None)
        if clock is not None:
            parsed_clocks.append(clock)
    result["trade_detail_time_parseable_count"] = len(parsed_clocks)
    result["trade_detail_time_unparseable_count"] = len(trade_rows) - len(parsed_clocks)
    if parsed_clocks:
        result["trade_detail_earliest_time_et"] = min(parsed_clocks)
        result["trade_detail_latest_time_et"] = max(parsed_clocks)
    session_matches = sum(1 for clock in parsed_clocks if _session_clock_match(clock, session_candidate))
    result["trade_detail_session_window_match_count"] = session_matches
    all_rows_in_window = bool(trade_rows) and len(parsed_clocks) == len(trade_rows) and session_matches == len(trade_rows)
    result["trade_detail_all_rows_in_candidate_session_window"] = all_rows_in_window

    if (
        result["application_status_success"] is True
        and result["error_class"] is None
        and last_update is not None
        and all_rows_in_window
    ):
        result["trade_date_candidate"] = last_update.strftime("%Y-%m-%d")
        result["trade_date_resolution_basis"] = (
            "explicit_last_update_et_date_plus_all_trade_row_clocks_inside_candidate_session_window"
        )
        result["trade_date_resolvable_without_hindsight"] = True
        latest_clock = result["trade_detail_latest_time_et"]
        if isinstance(latest_clock, str):
            latest_trade = datetime.strptime(
                f"{result['trade_date_candidate']} {latest_clock}", "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=ET)
            result["latest_trade_lag_seconds_vs_last_update"] = int(
                (last_update - latest_trade).total_seconds()
            )

    result["previous_info"] = _safe_sample(data.get("previousInfo"))
    return result


def _requested_sessions(now: datetime, force: str) -> list[str]:
    if force in {"premarket", "after_hours"}:
        return [force]
    hhmm = now.strftime("%H:%M")
    if "04:15" <= hhmm < "09:30":
        return ["premarket"]
    if "16:15" <= hhmm < "20:00":
        return ["after_hours"]
    return ["premarket", "after_hours"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/market-data-collector-access.yaml")
    parser.add_argument("--store-root", default="sources/market-data")
    parser.add_argument("--session", choices=["auto", "premarket", "after_hours"], default="auto")
    args = parser.parse_args()

    config = _load_yaml(args.config)
    spec: Mapping[str, Any] = config["nasdaq_extended_hours_probe"]
    timeout = int(config["http"]["timeout_seconds"])
    user_agent = str(config["http"]["user_agent"])
    now = datetime.now(ET)
    sessions = _requested_sessions(now, args.session)
    targets = _probe_targets(spec)
    candidates = spec.get("candidate_endpoints") or {}

    results: list[dict[str, Any]] = []
    for session in sessions:
        for candidate in candidates.get(session) or []:
            candidate_id = str(candidate["id"])
            template = str(candidate["url_template"])
            for target in targets:
                symbol = target["symbol"]
                asset_class = target["asset_class"]
                item = _probe_url(
                    template.format(symbol=symbol, asset_class=asset_class),
                    session_candidate=session,
                    timeout=timeout,
                    user_agent=user_agent,
                )
                item.update(
                    {
                        "session_candidate": session,
                        "candidate_id": candidate_id,
                        "symbol": symbol,
                        "asset_class": asset_class,
                    }
                )
                results.append(item)

    document = {
        "schema_version": 4,
        "probe": "nasdaq_extended_hours",
        "role": "shadow_transport_probe_only",
        "generated_at": now.isoformat(),
        "probe_calendar_date_et": now.strftime("%Y-%m-%d"),
        "requested_sessions": sessions,
        "targets": targets,
        "symbols": [target["symbol"] for target in targets],
        "market_fact_authority": False,
        "automatic_promotion_allowed": False,
        "results": results,
    }
    relative = (
        Path("collector-state/probes/nasdaq-extended")
        / now.strftime("%Y-%m")
        / now.strftime("%Y-%m-%d")
        / f"{now.strftime('%H%M%S')}-et.json"
    )
    path = Path(args.store_root) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    http_200_json = sum(
        1
        for item in results
        if item.get("http_status") == 200 and item.get("json_top_level_keys")
    )
    application_success_json = sum(
        1
        for item in results
        if item.get("http_status") == 200
        and item.get("json_top_level_keys")
        and item.get("application_status_success") is True
        and item.get("error_class") is None
    )
    trade_rows = sum(
        int(item.get("trade_detail_row_count") or 0)
        for item in results
        if item.get("application_status_success") is True
    )
    trade_date_resolvable = sum(
        1 for item in results if item.get("trade_date_resolvable_without_hindsight") is True
    )
    session_bounded = sum(
        1 for item in results if item.get("trade_detail_all_rows_in_candidate_session_window") is True
    )
    print(
        json.dumps(
            {
                "mode": "nasdaq_extended_probe",
                "status": "probe_recorded",
                "requested_sessions": sessions,
                "target_count": len(targets),
                "result_count": len(results),
                "http_200_json": http_200_json,
                "application_success_json": application_success_json,
                "trade_detail_rows": trade_rows,
                "trade_date_resolvable_results": trade_date_resolvable,
                "session_bounded_results": session_bounded,
                "path": str(relative),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
