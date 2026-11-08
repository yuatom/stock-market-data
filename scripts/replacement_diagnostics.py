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
REVISION_FIELDS = ('open', 'high', 'low', 'close', 'volume', 'source_timestamp')
VALUE_TYPES = frozenset({'missing', 'null', 'bool', 'int', 'float', 'str', 'list', 'dict', 'other'})
DIFFERENCE_KINDS = frozenset({'presence_changed', 'type_changed', 'value_changed', 'representation_changed'})


def _value_type(row, field):
    if field not in row:
        return 'missing'
    return {type(None): 'null', bool: 'bool', int: 'int', float: 'float',
            str: 'str', list: 'list', dict: 'dict'}.get(type(row[field]), 'other')


def summarize_revision_difference(old, new, canonical_bytes):
    """Metadata for one already-rejected row; never values, hashes or raw keys.

    The caller must retain its original rejection if this best-effort helper
    fails. Canonical comparison is intentional: Python equality hides int/float
    and signed-zero differences that the existing acceptance gate rejects.
    """
    fields = []
    for field in REVISION_FIELDS:
        old_type, new_type = _value_type(old, field), _value_type(new, field)
        if old_type == new_type == 'missing':
            continue
        if 'missing' in (old_type, new_type):
            kind = 'presence_changed'
        elif canonical_bytes(old[field]) == canonical_bytes(new[field]):
            continue
        elif old_type != new_type:
            kind = 'type_changed'
        elif old[field] != new[field]:
            kind = 'value_changed'
        else:
            kind = 'representation_changed'
        fields.append(dict(field=field, kind=kind, old_type=old_type, new_type=new_type))
    # Do not include even the names/counts/hashes of unknown fields. trade_date
    # is the existing gate's separately projected row identifier, not a change.
    known = set(REVISION_FIELDS) | {'trade_date'}
    old_extra = {key: value for key, value in old.items() if key not in known}
    new_extra = {key: value for key, value in new.items() if key not in known}
    return {'scope': 'first_rejected_row', 'fields': fields,
            'unlisted_field_difference': canonical_bytes(old_extra) != canonical_bytes(new_extra)}


def project_revision_difference(value):
    """Reproject at every boundary; malformed or empty evidence is absent."""
    if (type(value) is not dict or type(value.get('fields')) is not list or
            type(value.get('unlisted_field_difference')) is not bool):
        return None
    fields = {}
    for item in value['fields'][:len(REVISION_FIELDS)]:
        if type(item) is not dict:
            continue
        field, kind = item.get('field'), item.get('kind')
        old_type, new_type = item.get('old_type'), item.get('new_type')
        if (type(field) is str and field in REVISION_FIELDS and
                type(kind) is str and kind in DIFFERENCE_KINDS and
                type(old_type) is str and old_type in VALUE_TYPES and
                type(new_type) is str and new_type in VALUE_TYPES and field not in fields):
            fields[field] = dict(field=field, kind=kind, old_type=old_type, new_type=new_type)
    if not fields and not value['unlisted_field_difference']:
        return None
    return {'scope': 'first_rejected_row',
            'fields': [fields[field] for field in REVISION_FIELDS if field in fields],
            'unlisted_field_difference': value['unlisted_field_difference']}


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
    if out['rejection_rule'] == 'revision_outside_tail':
        # Observability must never replace the already-established gate/rule.
        try:
            difference = project_revision_difference(value.get('revision_difference'))
            if difference is not None:
                out['revision_difference'] = difference
        except Exception:
            pass
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
