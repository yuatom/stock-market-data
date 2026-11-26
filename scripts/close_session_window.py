"""Cash-equity collection and metric window from the pinned exchange calendar."""
from datetime import date
from reviewed_equity_calendar import _load


def candidate_close_window(day):
    """Override legacy time only on an explicitly listed 2026 early close.

    Report-calendar admission and task fire times are owned outside this
    proposed collector/proof window change.
    """
    d = date.fromisoformat(day)
    if d.isoformat() != day:
        raise ValueError("non-canonical session date")
    if d.year == 2026:
        _, _, early = _load(day)
        if day in early:
            return {"start":"12:45", "end":"13:00"}
    return {"start":"15:45", "end":"16:00"}
