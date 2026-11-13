#!/usr/bin/env python3
"""Deterministic free-source market-data collectors.

This module is the implementation behind collect_market_data.py. It never owns
report research, decision, projection, or canonical report finalization.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
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
        canonical_bytes,
        append_daily_bars,
        assert_legacy_basis,
        git_blob_sha_bytes,
        json_sha256,
        read_daily_series,
        write_capture,
        write_snapshot,
    )
except ImportError:  # pragma: no cover
    from scripts.market_data_store import (
        MarketDataConflict,
        canonical_bytes,
        append_daily_bars,
        assert_legacy_basis,
        git_blob_sha_bytes,
        json_sha256,
        read_daily_series,
        write_capture,
        write_snapshot,
    )

try:
    from scripts.eleven_symbol_append import classify_daily_overlap
except ModuleNotFoundError as exc:
    if exc.name != 'scripts': raise
    from eleven_symbol_append import classify_daily_overlap

ET = ZoneInfo("America/New_York")
TWELVE_PROVIDER = "twelve_data_basic"
NASDAQ_PROVIDER = "nasdaq_public_intraday"
MAX_CONFLICT_DIAGNOSTIC_RECORDS = 32
_DAILY_BAR_FIELDS = ("open", "high", "low", "close", "volume", "source_timestamp")


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


class _NoCaptureRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


def _read_capture_chunked(response: Any, maximum_bytes: int) -> bytes:
    """Validate framing before returning entity bytes; stdlib decoding is lenient."""
    token = rb"[!#$%&'*+\-.^_`|~0-9A-Za-z]+"
    quoted = rb'"(?:[\t\x20\x21\x23-\x5b\x5d-\x7e\x80-\xff]|\\[\t\x20-\x7e\x80-\xff])*"'
    size_line = re.compile(rb"([0-9A-Fa-f]+)(?:[ \t]*;[ \t]*" + token +
                           rb"(?:[ \t]*=[ \t]*(?:" + token + rb"|" + quoted + rb"))?)*\r\n")
    trailer_line = re.compile(rb"(" + token + rb"):[\t\x20-\x7e\x80-\xff]*\r\n")
    stream = response.fp
    if response.chunked is not True or stream is None:
        raise RuntimeError("raw transport chunk decoder unavailable")
    framing_bytes = 0

    def incomplete():
        raise http.client.IncompleteRead(b"")

    def read_line():
        nonlocal framing_bytes
        line = stream.readline(8193)
        framing_bytes += len(line)
        if len(line) > 8192 or not line.endswith(b"\r\n") or framing_bytes > 65536:
            incomplete()
        return line

    def read_exact(size):
        chunks = []
        while size:
            part = stream.read(size)
            if not part:
                incomplete()
            chunks.append(part)
            size -= len(part)
        return b"".join(chunks)

    body = bytearray()
    while True:
        match = size_line.fullmatch(read_line())
        if match is None:
            incomplete()
        size = int(match[1], 16)
        if size == 0:
            while True:
                line = read_line()
                if line == b"\r\n":
                    return bytes(body)
                trailer = trailer_line.fullmatch(line)
                if trailer is None or trailer[1].lower() in (b"content-length", b"transfer-encoding"):
                    incomplete()
        if size > maximum_bytes - len(body):
            raise RuntimeError("raw response byte bound rejected")
        body.extend(read_exact(size))
        framing_bytes += 2
        if framing_bytes > 65536 or read_exact(2) != b"\r\n":
            incomplete()


def _http_raw(url: str, *, timeout: int, user_agent: str, maximum_bytes: int) -> bytes:
    """Bounded capture transport; never follow a redirect carrying credentials.

    Existing JSON transport is unchanged. Caller owns authorization, budget,
    global serialization, evidence destination and all market qualification.
    """
    if (type(maximum_bytes) is not int or not 0 < maximum_bytes <= 4 * 1024 * 1024 or
        type(timeout) is not int or not 0 < timeout <= 120 or
        not isinstance(user_agent, str) or not user_agent or len(user_agent) > 256 or
        any(ord(char) < 32 or ord(char) > 126 for char in user_agent)):
        raise RuntimeError("invalid raw transport bounds")
    request = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept": "application/json"})
    opener = urllib.request.build_opener(_NoCaptureRedirect())
    with opener.open(request, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError("raw transport HTTP status rejected")
        media_types = response.headers.get_all("content-type", [])
        media_type = media_types[0].split(";", 1)[0].strip().lower() if len(media_types) == 1 else ""
        if not re.fullmatch(r"application/(?:json|[!#$%&'*+\-.^_`|~0-9a-z]+\+json)", media_type):
            raise RuntimeError("raw transport content type rejected")
        lengths = response.headers.get_all("content-length", [])
        encodings = response.headers.get_all("transfer-encoding", [])
        if (len(lengths) > 1 or len(encodings) > 1 or (lengths and encodings) or
            (encodings and encodings[0].lower() != "chunked")):
            raise RuntimeError("raw transport framing rejected")
        expected_length = None
        if lengths:
            value = lengths[0].strip()
            if not value.isascii() or not value.isdecimal() or len(value) > 10:
                raise RuntimeError("raw transport framing rejected")
            expected_length = int(value)
            if not 0 < expected_length <= maximum_bytes:
                raise RuntimeError("raw response byte bound rejected")
        raw = _read_capture_chunked(response, maximum_bytes) if encodings else response.read(maximum_bytes + 1)
        if expected_length is not None and len(raw) != expected_length:
            raise RuntimeError("raw transport incomplete body")
    if not raw or len(raw) > maximum_bytes:
        raise RuntimeError("raw response byte bound rejected")
    return raw


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
    deadline: float | None = None,
) -> None:
    while True:
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("budget wait deadline exceeded")
        path = _budget_path(store_root, trade_date)
        now = datetime.now(timezone.utc)
        minute = now.strftime("%Y-%m-%dT%H:%MZ")
        state = _load_budget_state(path, trade_date)
        if int(state.get("credits", 0)) >= per_day:
            raise BudgetExceeded(f"Twelve Data daily budget exhausted: {per_day}")
        if int((state.get("minutes") or {}).get(minute, 0)) < per_minute:
            return
        delay = max(1.0, 61.0 - now.second - now.microsecond / 1_000_000.0)
        if deadline is not None and time.monotonic() + delay >= deadline:
            raise TimeoutError("budget wait deadline exceeded")
        time.sleep(delay)


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


def _store_source_reference(store_root: Path, symbol: str, trade_date: str) -> dict[str, Any]:
    index_path = _series_index_path(store_root, TWELVE_PROVIDER, symbol)
    index_bytes = index_path.read_bytes()
    index = json.loads(index_bytes)
    row_number = None
    shard_ref = None
    for shard in index.get("shards", []):
        shard_path = index_path.parent / str(shard["path"])
        shard_doc = json.loads(shard_path.read_text(encoding="utf-8"))
        for offset, row in enumerate(shard_doc.get("records", [])):
            if row.get("trade_date") == trade_date:
                row_number = int(shard["start"]) + offset
                shard_ref = {
                    "path": shard_path.relative_to(store_root).as_posix(),
                    "blob_sha": git_blob_sha_bytes(shard_path.read_bytes()),
                }
                break
        if shard_ref:
            break
    return {
        "provider": TWELVE_PROVIDER,
        "index_path": index_path.relative_to(store_root).as_posix(),
        "index_blob_sha": git_blob_sha_bytes(index_bytes),
        "row_number": row_number,
        "shard": shard_ref,
    }


def _conflict_record(
    *, store_root: Path, symbol: str, old: Mapping[str, Any], new: Mapping[str, Any],
    outputsize: int, effective_date: str | None, trade_date: str,
    access: Mapping[str, Any],
) -> dict[str, Any]:
    differences = []
    for field in _DAILY_BAR_FIELDS:
        old_value = old.get(field)
        new_value = new.get(field)
        if type(old_value) is not type(new_value) or old_value != new_value:
            differences.append({
                "field": field,
                "old_type": type(old_value).__name__,
                "old_value": old_value,
                "old_sha256": json_sha256({"type": type(old_value).__name__, "value": old_value}),
                "new_type": type(new_value).__name__,
                "new_value": new_value,
                "new_sha256": json_sha256({"type": type(new_value).__name__, "value": new_value}),
            })
    return {
        "symbol": symbol,
        "trade_date": str(new["trade_date"]),
        "fields": differences,
        "old_row_sha256": json_sha256(dict(old)),
        "new_row_sha256": json_sha256(dict(new)),
        "store_source": _store_source_reference(store_root, symbol, str(new["trade_date"])),
        "request_parameters": {
            "provider": TWELVE_PROVIDER,
            "endpoint_host": urllib.parse.urlparse(str(access["twelve_data_basic"]["base_url"])).hostname,
            "endpoint_path": str(access["twelve_data_basic"]["time_series_path"]),
            "symbol": symbol,
            "interval": str(access["twelve_data_basic"]["interval_daily"]),
            "outputsize": outputsize,
            "format": "JSON",
            "trade_date_exclusive_upper_bound": trade_date,
            "identity_effective_from": effective_date,
        },
    }


def _write_private_eod_diagnostic(
    *, store_root: Path, trade_date: str, result: dict[str, Any], diagnostics: list[dict[str, Any]],
    conflict_count: int,
) -> None:
    request_id = os.environ.get("MARKET_DATA_REQUEST_ID", "")
    invocation_id = "-".join(filter(None, (
        os.environ.get("GITHUB_RUN_ID"), os.environ.get("GITHUB_RUN_ATTEMPT")
    ))) or request_id or "local"
    token = hashlib.sha256(invocation_id.encode("utf-8")).hexdigest()[:16]
    relative = Path("collector-state") / trade_date[:7] / f"{trade_date}-previous_session_eod-{token}-conflicts.json"
    payload = {
        "schema_version": 1,
        "diagnostic_authority": "non_authoritative_conflict_evidence",
        "trade_date": trade_date,
        "invocation": {
            "request_id": request_id or None,
            "invocation_id": invocation_id,
            "repository": os.environ.get("GITHUB_REPOSITORY"),
            "workflow": os.environ.get("GITHUB_WORKFLOW"),
            "run_id": os.environ.get("GITHUB_RUN_ID"),
            "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
            "event": os.environ.get("GITHUB_EVENT_NAME"),
            "mode": "previous_session_eod",
        },
        "compute_sha": os.environ.get("MARKET_DATA_COMPUTE_SHA"),
        "store_input_sha": os.environ.get("MARKET_DATA_STORE_INPUT_SHA"),
        "symbols_requested": result.get("symbols_requested", 0),
        "failure_target_count": len(result.get("failures", [])),
        "conflict_record_count": conflict_count,
        "conflict_diagnostic_record_count": len(diagnostics),
        "conflict_diagnostics_truncated": conflict_count > len(diagnostics),
        "conflicts": diagnostics,
    }
    destination = store_root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    result["result_state_path"] = relative.as_posix()


def _load_daily_basis_owner(store_root, trade_date):
    filename = os.environ.get("DAILY_BASIS_DELEGATION_FILE", "")
    if not filename:
        return {}
    import sys
    repository_root = str(Path(__file__).resolve().parents[1])
    if repository_root not in sys.path:
        sys.path.insert(0, repository_root)
    from scripts.daily_basis_delegation import load_daily_basis_owner
    return load_daily_basis_owner(store_root, trade_date, filename, dict(os.environ))


def collect_previous_session_eod(
    *,
    trade_date: str,
    store_root: Path,
    watchlist: Mapping[str, Any],
    completeness: Mapping[str, Any],
    config: Mapping[str, Any],
    access: Mapping[str, Any],
) -> dict[str, Any]:
    # New behavior requires an explicitly deployed policy; absent preserves legacy.
    revision_policy=config['collector']['twelve_data_daily'].get('overlap_revision_policy')
    if revision_policy not in (None,'price_only_volume_blocked_v1'):
        raise ValueError('unsupported daily revision policy')
    price_only=revision_policy=='price_only_volume_blocked_v1'
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
    conflict_diagnostics: list[dict[str, Any]] = []
    conflict_record_count = 0
    volume_revision_count = 0
    representation_only_count = 0
    universe = _daily_universe(watchlist, completeness)
    daily_basis_owner = _load_daily_basis_owner(store_root, trade_date)
    delegated_daily_symbols = []

    for symbol, asset_class in universe:
        if symbol in daily_basis_owner:
            delegated_daily_symbols.append({"symbol": symbol, "status": daily_basis_owner[symbol]})
            continue
        phase = "budget_check"
        try:
            assert_legacy_basis(store_root, provider=TWELVE_PROVIDER, symbol=symbol)
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
            phase = "provider_fetch"
            fetched = fetch_twelve_daily(symbol, key, access, outputsize=outputsize)
            if price_only:
                # Retain all normalized response rows BEFORE any comparison rejection.
                # This is not original HTTP bytes and does not recover past responses.
                retained=dict(schema_version=1,kind='normalized_daily_price_only_evidence',
                              symbol=symbol,execution_day=trade_date,records=fetched,
                              volume_policy='BLOCKED_UNRESOLVED')
                payload=canonical_bytes(retained)
                evidence_digest=hashlib.sha256(payload).hexdigest()
                relative=Path('collector-state')/trade_date[:7]/('price-only-'+evidence_digest+'.json')
                evidence_path=relative.as_posix()
                target=store_root/relative
                target.parent.mkdir(parents=True,exist_ok=True)
                with target.open('xb') as handle: handle.write(payload)
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

            if price_only:
                phase='price_only_quality_validation'
                if any(not classify_daily_overlap(dict(row),dict(row))['price_eligible'] for row in eligible):
                    raise MarketDataConflict('price-only daily response quality invalid')
            overlap_classifications=[]
            index_path = _series_index_path(store_root, TWELVE_PROVIDER, symbol)
            existing: list[dict[str, Any]] = []
            if index_path.exists():
                phase = "stored_series_read"
                existing = read_daily_series(store_root, provider=TWELVE_PROVIDER, symbol=symbol)
                existing_by_date = {row["trade_date"]: row for row in existing}
                phase = "historical_overlap_comparison"
                has_conflict = False
                for row in eligible:
                    old = existing_by_date.get(row["trade_date"])
                    if old is not None:
                        classification=classify_daily_overlap(dict(old),dict(row))
                        overlap_classifications.append(dict(trade_date=row['trade_date'],**classification))
                        if price_only and classification['classification']=='VOLUME_ONLY':
                            volume_revision_count += 1
                        if price_only and classification['classification']=='EQUIVALENT' and canonical_bytes(old)!=canonical_bytes(row):
                            representation_only_count += 1
                    rejected=(old is not None and
                              (not classification['price_eligible'] if price_only else canonical_bytes(old)!=canonical_bytes(row)))
                    if rejected:
                        has_conflict = True
                        conflict_record_count += 1
                        if len(conflict_diagnostics) < MAX_CONFLICT_DIAGNOSTIC_RECORDS:
                            conflict_diagnostics.append(_conflict_record(
                                store_root=store_root,
                                symbol=symbol,
                                old=old,
                                new=row,
                                outputsize=outputsize,
                                effective_date=effective,
                                trade_date=trade_date,
                                access=access,
                            ))
                if has_conflict:
                    raise MarketDataConflict("provider historical revision detected")
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

            lineage={"ingest_kind":"collector_previous_session_eod",
                     "ingest_trade_date":trade_date,"history_outputsize":outputsize}
            if price_only:
                lineage.update(ingest_kind='collector_price_only_revision_v1',
                               volume_policy='BLOCKED_UNRESOLVED',
                               normalized_evidence_path=evidence_path,
                               normalized_evidence_sha256=evidence_digest)
            phase = "immutable_append"
            result = append_daily_bars(
                store_root,
                provider=TWELVE_PROVIDER,
                symbol=symbol,
                asset_class=asset_class,
                bars=new_rows,
                identity_effective_from=effective,
                series_semantics="daily_regular_ohlcv",
                adjustment_semantics="provider_reported",
                lineage=lineage,
            )
            if result.changed:
                changed_series += 1
                if not existing:
                    bootstrapped_series += 1
        except BudgetExceeded:
            raise
        except Exception as exc:
            failure = {"symbol": symbol, "reason": type(exc).__name__, "detail": str(exc)[:240]}
            if isinstance(exc, MarketDataConflict):
                # Classify only by the proven call phase, never by private provider data
                # or a substring of an exception message. Do not relax append-only.
                failure["reason_code"] = {
                    "stored_series_read": "stored_series_integrity_conflict",
                    "price_only_quality_validation": "daily_price_quality_invalid",
                    "historical_overlap_comparison": "historical_provider_revision",
                    "immutable_append": "immutable_append_conflict",
                }.get(phase, "unclassified_market_data_conflict")
            failures.append(failure)

    result = {
        "mode": "previous_session_eod",
        "status": "ok" if not failures else "partial",
        "symbols_requested": len(universe),
        "changed_series": changed_series,
        "bootstrapped_series": bootstrapped_series,
        "failure_target_count": len(failures),
        "conflict_record_count": conflict_record_count,
        **({"price_only_volume_revision_count":volume_revision_count,
            "numeric_representation_only_count":representation_only_count,
            "volume_policy":"BLOCKED_UNRESOLVED"} if price_only else {}),
        "conflict_diagnostic_record_count": len(conflict_diagnostics),
        "conflict_diagnostics_truncated": conflict_record_count > len(conflict_diagnostics),
        "conflict_diagnostics": conflict_diagnostics,
        "failures": failures,
        **({"delegated_daily_symbols": delegated_daily_symbols} if delegated_daily_symbols else {}),
    }
    _write_private_eod_diagnostic(
        store_root=store_root,
        trade_date=trade_date,
        result=result,
        diagnostics=conflict_diagnostics,
        conflict_count=conflict_record_count,
    )
    return result


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
