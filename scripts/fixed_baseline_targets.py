"""Fail-closed selectors for explicit fixed-universe daily-history maintenance."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from datetime import date
from pathlib import Path
from typing import Any

HEX40 = re.compile(r"^[0-9a-f]{40}$")
SYMBOL = re.compile(r"^[A-Z][A-Z0-9.-]{0,9}$")
INDEX_PREFIX = "data/market-data/series/daily/twelve_data_basic"


class FixedTargetError(ValueError):
    """An explicit fixed-universe selector cannot be proven against the pinned Store."""


def _unique_object(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate object member")
        out[key] = value
    return out


def parse_fixed_selection(raw: str | None) -> dict | None:
    """Parse a selector; omission preserves the caller's historical default."""
    if raw is None:
        return None
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, TypeError, json.JSONDecodeError):
        raise FixedTargetError("fixed target selection invalid") from None


def validate_selection_mix(*, fixed_selector: dict | None, dynamic_selector: dict | None) -> None:
    if fixed_selector is not None and dynamic_selector is not None:
        raise FixedTargetError("fixed and dynamic target selection cannot be combined")


def _git(repo: Path, *args: str) -> str:
    env = dict(os.environ)
    env.pop("TWELVE_DATA_API_KEY", None)
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, env=env, timeout=90,
    )
    if result.returncode:
        raise FixedTargetError("pinned Store identity unavailable")
    return result.stdout.strip()


def _exact_file(repo: Path, commit: str, relative: str, blob: str) -> bytes:
    if not isinstance(blob, str) or not HEX40.fullmatch(blob):
        raise FixedTargetError("fixed target selection invalid")
    if _git(repo, "rev-parse", f"{commit}:{relative}") != blob:
        raise FixedTargetError("fixed target index identity mismatch")
    path = repo / relative
    if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(repo.resolve()):
        raise FixedTargetError("fixed target index path invalid")
    for part in (path, *path.parents):
        if part == repo:
            break
        if part.is_symlink():
            raise FixedTargetError("fixed target index path invalid")
    raw = path.read_bytes()
    actual = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
    if actual != blob:
        raise FixedTargetError("fixed target index bytes mismatch")
    return raw


def resolve_fixed_targets(h, *, store_repo: Path, store_sha: str,
                          selector: dict[str, Any], universe: dict) -> list[dict]:
    """Resolve an exact nonempty fixed-universe subset before any budget or HTTP call."""
    try:
        if (not isinstance(selector, dict) or set(selector) != {"schema_version", "targets"}
                or type(selector.get("schema_version")) is not int or selector["schema_version"] != 1
                or not isinstance(selector.get("targets"), list) or not selector["targets"]):
            raise ValueError
        if not isinstance(store_sha, str) or not HEX40.fullmatch(store_sha):
            raise ValueError
        if _git(store_repo, "rev-parse", "HEAD") != store_sha:
            raise ValueError
        available = {
            symbol: asset for symbol, asset in h.collection.daily_universe(universe)
        }
        if not available or len(available) != len(h.collection.daily_universe(universe)):
            raise ValueError
        symbols = []
        resolved = []
        for target in selector["targets"]:
            if not isinstance(target, dict) or set(target) != {"symbol", "index_blob_sha"}:
                raise ValueError
            symbol, blob = target["symbol"], target["index_blob_sha"]
            if not isinstance(symbol, str) or not SYMBOL.fullmatch(symbol) or symbol not in available:
                raise ValueError
            if symbol in symbols:
                raise ValueError
            relative = f"{INDEX_PREFIX}/{symbol}/_index.json"
            raw = _exact_file(store_repo, store_sha, relative, blob)
            index = json.loads(raw)
            asset = available[symbol]
            effective = h.collection.ticker_effective_at(universe, symbol)
            expected = {
                "provider": "twelve_data_basic", "symbol": symbol, "asset_class": asset,
                "session": "regular", "interval": "1d",
                "series_semantics": "daily_regular_ohlcv",
                "adjustment_semantics": "provider_reported",
                "series_id": f"twelve_data_basic:{symbol}:regular:1d",
                "identity_effective_from": effective,
            }
            if not isinstance(index, dict) or any(index.get(key) != value for key, value in expected.items()):
                raise ValueError
            if effective is not None and date.fromisoformat(effective).isoformat() != effective:
                raise ValueError
            symbols.append(symbol)
            resolved.append({
                "symbol": symbol, "asset_class": asset, "effective": effective,
                "index_blob_sha": blob,
            })
        return resolved
    except Exception:
        raise FixedTargetError("fixed target selection invalid") from None
