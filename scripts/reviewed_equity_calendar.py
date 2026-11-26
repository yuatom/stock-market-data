"""Bounded published schedules, not proof that a market actually opened."""
from datetime import date, timedelta
import json
from pathlib import Path


def _load(day):
    target = date.fromisoformat(day)
    if target.isoformat() != day:
        raise ValueError("invalid execution date")
    path = Path(__file__).resolve().parents[1] / 'config' / f'us-equity-scheduled-sessions-{target.year}.json'
    def no_duplicate_keys(pairs):
        result = {}
        for k, v in pairs:
            if k in result:
                raise ValueError("duplicate schedule key")
            result[k] = v
        return result
    value = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=no_duplicate_keys)
    if (type(value['schema_version']) is not int or value['schema_version'] != 1
            or value['year'] != target.year or
            value['scope'] != 'US-listed equities regular session, America/New_York'):
        raise ValueError("invalid US equities schedule")
    if not isinstance(value['sources'], list) or len(set(value['sources'])) != 2:
        raise ValueError("schedule lacks two source references")
    def day_set(rows):
        if type(rows) is not list or not rows or rows != sorted(set(rows)):
            raise ValueError("invalid schedule date list")
        for item in rows:
            d = date.fromisoformat(item)
            if d.year != target.year or d.isoformat() != item or d.weekday() >= 5:
                raise ValueError("invalid schedule date")
        return frozenset(rows)
    closed = day_set(value['scheduled_closed'])
    early = day_set(value['scheduled_early_close'])
    if closed & early:
        raise ValueError("closed day also marked early-close")
    return target, closed, early


def missing_repair_sessions(day):
    """Scheduled dates within the 100-calendar-day lookback, never today's."""
    try:
        target, closed, _ = _load(day)
        days = (target - timedelta(days=n) for n in range(1, 101))
        return frozenset(d.isoformat() for d in days
                         if d.year == target.year and d.weekday() < 5 and d.isoformat() not in closed)
    except (OSError, ValueError, TypeError, KeyError):
        return frozenset()


def scheduled_close(day):
    """Published regular equities close, or None if closed or unavailable."""
    try:
        target, closed, early = _load(day)
        if target.weekday() >= 5 or day in closed:
            return None
        return '13:00' if day in early else '16:00'
    except (OSError, ValueError, TypeError, KeyError):
        return None
