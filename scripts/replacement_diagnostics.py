"""Value-free projection of replacement rejection evidence, never raw errors."""
from dataclasses import dataclass
from datetime import date
import re

RULES = frozenset({'unclassified', 'invalid_replacement_bar', 'insufficient_replacement_records',
    'existing_series_empty', 'replacement_empty', 'duplicate_replacement_dates',
    'missing_existing_dates', 'revision_outside_tail', 'revised_overlap_limit',
    'replacement_hash_mismatch'})
COUNT_FIELDS = ('existing_records', 'replacement_records', 'target_records',
                'missing_existing_count', 'revised_overlap_records')


def safe_date(value):
    if type(value) is not str or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value):
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def project_rejection(value):
    value = value if type(value) is dict else {}
    rule = value.get('rejection_rule')
    out = {'rejection_rule': rule if type(rule) is str and rule in RULES else 'unclassified'}
    for key in COUNT_FIELDS:
        count = value.get(key)
        if type(count) is int and 0 <= count <= 5000:
            out[key] = count
    dates = value.get('missing_existing_dates')
    if type(dates) is list:
        out['missing_existing_dates'] = [day for raw in dates[:8] if (day := safe_date(raw)) is not None]
    day = safe_date(value.get('revision_date'))
    if day is not None:
        out['revision_date'] = day
    return out


@dataclass(frozen=True)
class ReplacementDiagnostic:
    # Constructed only at the recovery qualification boundary from validated
    # entries/selection, never from provider data or exception symbol attributes.
    symbol: str
    selected_symbols: tuple[str, ...]
    evidence: dict

    def as_dict(self):
        out = {'replacement_rejection': project_rejection(self.evidence)}
        if (type(self.symbol) is str and re.fullmatch(r'[A-Z][A-Z0-9.-]{0,9}', self.symbol)
                and type(self.selected_symbols) is tuple
                and any(type(value) is str and self.symbol == value for value in self.selected_symbols)):
            out['failed_symbol'] = self.symbol
        return out
