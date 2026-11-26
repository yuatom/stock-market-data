"""Prepare an explicitly reviewed field-level revision from retained daily data.

Pure operation: no provider requests, credentials, publication or new dates.
The caller must independently authenticate exact packets and comparative
evidence, and use a reviewed publication/archival owner for any write.
"""
from copy import deepcopy
from datetime import date
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
import hashlib
import json

PRICE_FIELDS = frozenset(("open", "high", "low", "close"))

class CorrectionError(ValueError):
    pass

def row_sha256(row):
    raw = json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()

def _prices(row):
    try:
        vals = {k: Decimal(str(row[k])) for k in PRICE_FIELDS}
        if any(not n.is_finite() or n <= 0 for n in vals.values()):
            raise ValueError
        if not vals["low"] <= min(vals["open"], vals["close"]) <= max(vals["open"], vals["close"]) <= vals["high"]:
            raise ValueError
        return vals
    except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
        raise CorrectionError("invalid regular-session OHLC") from exc

def _cents(number):
    try:
        return number.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise CorrectionError("invalid comparison precision") from exc

def _dates(rows):
    result = {}
    if not isinstance(rows, list) or not rows:
        raise CorrectionError("candidate series is empty")
    for row in rows:
        try:
            day = row["trade_date"]
            valid = (date.fromisoformat(day).isoformat() == day and day not in result)
        except (KeyError, ValueError, TypeError):
            valid = False
        if not valid:
            raise CorrectionError("date missing, invalid or duplicate")
        result[day] = row
    if list(result) != sorted(result):
        raise CorrectionError("dates are not in ascending order")
    return result

def prepare(existing, retained, revisions, *, max_rows, tail_sessions):
    """Return changed copies and exact field history; verification is not publication."""
    if (type(max_rows) is not int or not 1 <= max_rows <= 3 or
        type(tail_sessions) is not int or not 1 <= tail_sessions <= 3 or
        not isinstance(revisions, list) or not 1 <= len(revisions) <= max_rows):
        raise CorrectionError("revision scope exceeds the guarded limit")
    original = _dates(existing)
    packet = _dates(retained)
    if not set(original) <= set(packet):
        raise CorrectionError("retained packet omits an existing date")
    allowed_days = set(list(original)[-tail_sessions:])
    requested = _dates(revisions)
    if not set(requested) <= allowed_days:
        raise CorrectionError("revision is not for an existing date in the permitted tail")
    out, audit = deepcopy(existing), []
    for idx, (day, old) in enumerate(original.items()):
        new = packet[day]
        old_px, new_px = _prices(old), _prices(new)
        changed = {field for field in PRICE_FIELDS if _cents(old_px[field]) != _cents(new_px[field])}
        reviewed = requested.get(day)
        if reviewed is None:
            if changed:
                raise CorrectionError("unreviewed price revision")
            continue
        if set(reviewed) != {"trade_date", "expected_old_row_sha256", "fields"} or reviewed["expected_old_row_sha256"] != row_sha256(old):
            raise CorrectionError("reviewed row identity changed")
        proof = reviewed["fields"]
        if not changed or not isinstance(proof, dict) or set(proof) != changed:
            raise CorrectionError("reviewed field set differs from observed price revision")
        fields = {}
        for field in sorted(changed):
            try:
                amount = proof[field]
                if not isinstance(amount, str) or Decimal(amount) != _cents(new_px[field]):
                    raise ValueError
            except (ValueError, InvalidOperation) as exc:
                raise CorrectionError("independent close or price confirmation differs") from exc
            if type(new[field]) not in (int, float):
                raise CorrectionError("retained price is not a JSON number")
            fields[field] = {"old": old[field], "new": new[field]}
            out[idx][field] = new[field]
        _prices(out[idx])
        audit.append({"trade_date": day, "changes": fields, "old_row_sha256": row_sha256(old),
                      "new_row_sha256": row_sha256(out[idx])})
    if len(audit) != len(revisions):
        raise CorrectionError("not all verified revisions were applied")
    return out, audit
