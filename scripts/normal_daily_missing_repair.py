"""Plan field fills from qualified ordinary data; does not publish history."""
from copy import deepcopy
from datetime import date
import math

OHLC = ('open','high','low','close')

class MissingRepairError(ValueError):
    pass

def _number(value, *, is_volume=False):
    if type(value) not in (int,float) or not math.isfinite(value):
        return False
    return value >= 0 if is_volume else value > 0

def _validate_bar(row):
    if any(not _number(row.get(k)) for k in OHLC):
        raise MissingRepairError('regular-session OHLC not qualified')
    if not row['low'] <= min(row['open'],row['close']) <= max(row['open'],row['close']) <= row['high']:
        raise MissingRepairError('a fill would yield invalid OHLC')

def fill_missing_regular_daily_fields(existing, incoming, *, allow_verified_volume=False):
    """Require same series/provider/session externally; existing non-null values never change.

    Absence or null is a missing value. A non-null malformed value still needs
    explicit reconciliation. The caller must not enable volume for quarantined
    series or infer volume qualification merely from a numerical payload.
    """
    d=existing.get('trade_date')
    try:
        if date.fromisoformat(d).isoformat()!=d or incoming.get('trade_date')!=d:
            raise ValueError
    except (TypeError,ValueError) as exc:
        raise MissingRepairError('regular-session date identity mismatch') from exc
    if type(allow_verified_volume) is not bool:
        raise MissingRepairError('volume qualification must be explicit')
    _validate_bar(incoming)
    result=deepcopy(existing)
    fills=[]
    conflicts=[]
    for k in (*OHLC, *(['volume'] if allow_verified_volume else [])):
        old,new=existing.get(k),incoming.get(k)
        if not _number(new,is_volume=k=='volume'):
            raise MissingRepairError('new value is not qualified')
        if old is None:
            result[k]=new
            fills.append(k)
        elif not _number(old,is_volume=k=='volume'):
            raise MissingRepairError('non-null invalid history is not an automatic fill')
        elif old!=new:
            conflicts.append(k)
    _validate_bar(result)
    return result, {'trade_date':d,'filled':fills,'preserved_conflicts':conflicts}
