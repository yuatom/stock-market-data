#!/usr/bin/env python3
"""Pure normalization helpers for a future Nasdaq extended-hours market-data adapter.

This module intentionally has no CLI, no network transport, no Store write and no
provider activation.  It only converts an already-fetched Nasdaq extended-trading
JSON payload into strictly validated candidate trade facts.  Production use still
requires an explicit provider/source-contract promotion after the configured
shadow-probe readiness gate passes.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Mapping
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
SUPPORTED_SESSIONS = {"premarket", "after_hours"}
SUPPORTED_ASSET_CLASSES = {"stocks", "etf"}


class ExtendedHoursAdapterError(ValueError):
    pass


def _rows(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Mapping) or not isinstance(value.get("rows"), list):
        return []
    rows = value["rows"]
    if not all(isinstance(row, Mapping) for row in rows):
        raise ExtendedHoursAdapterError("tradeDetailTable.rows must contain objects only")
    return list(rows)


def _parse_trade_clock(value: Any) -> str:
    if not isinstance(value, str):
        raise ExtendedHoursAdapterError("trade time must be a string")
    text = value.strip().upper()
    for fmt in ("%H:%M:%S", "%I:%M:%S %p", "%I:%M %p"):
        try:
            return datetime.strptime(text, fmt).strftime("%H:%M:%S")
        except ValueError:
            continue
    raise ExtendedHoursAdapterError(f"unparseable trade time: {value!r}")


def _parse_last_update(value: Any) -> datetime:
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
    raise ExtendedHoursAdapterError("explicit Nasdaq last-update ET timestamp is required")


def _parse_numeric(value: Any, *, field: str, non_negative: bool = False) -> float:
    if isinstance(value, bool):
        raise ExtendedHoursAdapterError(f"{field} must be numeric")
    if isinstance(value, (int, float)):
        parsed = float(value)
    elif isinstance(value, str):
        text = value.strip().replace(",", "").replace("$", "")
        if not text:
            raise ExtendedHoursAdapterError(f"{field} is empty")
        try:
            parsed = float(text)
        except ValueError as exc:
            raise ExtendedHoursAdapterError(f"{field} is not numeric: {value!r}") from exc
    else:
        raise ExtendedHoursAdapterError(f"{field} must be numeric")
    if non_negative and parsed < 0:
        raise ExtendedHoursAdapterError(f"{field} must be non-negative")
    return parsed


def _clock_in_session(clock: str, session: str) -> bool:
    if session == "premarket":
        return "04:00:00" <= clock < "09:30:00"
    if session == "after_hours":
        return "16:00:00" <= clock < "20:00:00"
    return False


def normalize_extended_trade_payload(
    payload: Mapping[str, Any],
    *,
    session: str,
    symbol: str,
    asset_class: str,
    endpoint_id: str,
) -> dict[str, Any]:
    """Return validated candidate facts without granting market-fact authority.

    The caller must already know which endpoint was used and must later bind any
    persisted fact to an explicitly promoted source contract.  This function does
    not infer or assert provider eligibility.
    """
    if session not in SUPPORTED_SESSIONS:
        raise ExtendedHoursAdapterError(f"unsupported session: {session}")
    symbol = str(symbol).strip().upper()
    if not symbol:
        raise ExtendedHoursAdapterError("symbol is required")
    asset_class = str(asset_class).strip().lower()
    if asset_class not in SUPPORTED_ASSET_CLASSES:
        raise ExtendedHoursAdapterError(f"unsupported asset_class: {asset_class}")
    if not endpoint_id:
        raise ExtendedHoursAdapterError("endpoint_id is required")
    if not isinstance(payload, Mapping):
        raise ExtendedHoursAdapterError("payload must be an object")

    status = payload.get("status")
    if not isinstance(status, Mapping):
        raise ExtendedHoursAdapterError("Nasdaq application status object is required")
    try:
        rcode = int(status.get("rCode"))
    except (TypeError, ValueError) as exc:
        raise ExtendedHoursAdapterError("Nasdaq application rCode is invalid") from exc
    if rcode != 200:
        raise ExtendedHoursAdapterError(f"Nasdaq application rCode is not success: {rcode}")

    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise ExtendedHoursAdapterError("Nasdaq data object is required")
    last_update = _parse_last_update(data.get("lastUpdateInfo"))
    trade_date = last_update.date().isoformat()
    rows = _rows(data.get("tradeDetailTable"))
    if not rows:
        raise ExtendedHoursAdapterError("at least one extended-hours trade row is required")

    facts: list[dict[str, Any]] = []
    for row in rows:
        clock = _parse_trade_clock(row.get("time"))
        if not _clock_in_session(clock, session):
            raise ExtendedHoursAdapterError(
                f"trade clock {clock} is outside declared {session} window"
            )
        event_time = datetime.strptime(
            f"{trade_date} {clock}", "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=ET)
        lag_seconds = int((last_update - event_time).total_seconds())
        if lag_seconds < 0:
            raise ExtendedHoursAdapterError(
                f"trade event {event_time.isoformat()} is later than last update {last_update.isoformat()}"
            )
        facts.append(
            {
                "symbol": symbol,
                "asset_class": asset_class,
                "session": session,
                "event_time": event_time.isoformat(),
                "source_timestamp": event_time.isoformat(),
                "last_sale": _parse_numeric(row.get("price"), field="price"),
                "reported_share_volume": _parse_numeric(
                    row.get("shareVolume"), field="shareVolume", non_negative=True
                ),
                "observed_delay_seconds": lag_seconds,
                "endpoint_id": endpoint_id,
            }
        )

    facts.sort(key=lambda fact: (fact["event_time"], fact["last_sale"], fact["reported_share_volume"]))
    latest_event = max(datetime.fromisoformat(fact["event_time"]) for fact in facts)
    return {
        "schema_version": 1,
        "market_fact_authority": False,
        "promotion_required": True,
        "trade_date": trade_date,
        "session": session,
        "symbol": symbol,
        "asset_class": asset_class,
        "endpoint_id": endpoint_id,
        "last_update_timestamp_et": last_update.isoformat(),
        "actual_data_cutoff": latest_event.isoformat(),
        "qualified_candidate_facts": facts,
    }
