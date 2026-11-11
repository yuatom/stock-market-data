"""Pure offline intake. No HTTP, filesystem, credentials, Store, or qualification.

Hashes detect content changes against a separately retained anchor. They never
prove provider origin, authorized collection, observation time, or market truth.
"""
import base64
import binascii
import hashlib
import json
import re
from datetime import date, datetime
from decimal import Decimal

MAX_RAW_BYTES = 4 * 1024 * 1024
MAX_ENVELOPE_BYTES = 12 * 1024 * 1024
FIELDS = ('open', 'high', 'low', 'close', 'volume')
REQUEST_KEYS = set('provider symbol interval exchange mic_code currency type adjust dp start_date end_date outputsize order'.split())
META_KEYS = set('symbol interval currency exchange_timezone exchange mic_code type'.split())
CLAIMS = dict.fromkeys(('source_authenticated', 'observation_time_verified',
    'market_qualified', 'rounding_verified', 'volume_semantics_verified',
    'calendar_verified', 'production_activation'), False)


class EvidenceError(ValueError):
    """Malformed, ambiguous or unverifiable local evidence."""


def _require(condition, message):
    if not condition:
        raise EvidenceError(message)


def _closed(value, keys, label):
    _require(type(value) is dict and set(value) == keys, label + ' schema mismatch')


def _day(value):
    _require(type(value) is str and re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value), 'invalid date')
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise EvidenceError('invalid date') from exc


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, 'duplicate JSON key')
        result[key] = value
    return result


def _constant(_):
    raise EvidenceError('nonfinite JSON constant')


def _load(raw, bound):
    _require(type(raw) is bytes and 0 < len(raw) <= bound, 'bounded bytes required')
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs, parse_constant=_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise EvidenceError('invalid or ambiguous JSON') from exc


def _encode(value):
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False)+'\n').encode('ascii')


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def validate_request(request: dict) -> dict:
    _closed(request, REQUEST_KEYS, 'request')
    for key, expected in [('provider','twelve_data'),('interval','1day'),('type','ETF'),('adjust','splits'),('order','asc'),
                          ('exchange','NASDAQ'),('currency','USD')]:
        _require(request[key] == expected, 'unsupported request '+key)
    # Explicit SOXX profile support, not an XNAS/XNMS alias. The response
    # identity must still equal the exact request below. Public source:
    # https://twelvedata.com/markets/256481/etf/nasdaq/soxx (2026-11-11).
    allowed_mics = ('XNAS', 'XNMS') if request['symbol'] == 'SOXX' else ('XNAS',)
    _require(request['mic_code'] in allowed_mics, 'unsupported request mic_code')
    for key, pattern in [('symbol',r'[A-Z][A-Z0-9.-]{0,14}'),('exchange',r'[A-Z][A-Z0-9 ]{0,30}'),
                         ('mic_code',r'[A-Z0-9]{4}'),('currency',r'[A-Z]{3}')]:
        _require(type(request[key]) is str and re.fullmatch(pattern, request[key]), 'invalid identity '+key)
    _require(type(request['dp']) is int and -1 <= request['dp'] <= 11, 'explicit dp required')
    _require(type(request['outputsize']) is int and 1 <= request['outputsize'] <= 5000, 'invalid outputsize')
    start, end = _day(request['start_date']), _day(request['end_date'])
    _require(start <= end, 'reversed request dates')
    return json.loads(_encode(request))


def _inspect(raw, request, observed_at):
    request = validate_request(request)
    start, end = _day(request['start_date']), _day(request['end_date'])
    _require(type(observed_at) is str and len(observed_at) <= 40 and
             re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})', observed_at),
             'aware observation timestamp required')
    if not observed_at.endswith('Z'):
        _require(int(observed_at[-2:]) <= 59 and int(observed_at[-5:-3]) <= 23, 'invalid timezone offset')
    try:
        timestamp = datetime.fromisoformat(observed_at)
        _require(timestamp.utcoffset() is not None, 'aware observation timestamp required')
    except ValueError as exc:
        raise EvidenceError('invalid observation timestamp') from exc
    doc = _load(raw, MAX_RAW_BYTES)
    _closed(doc, {'meta','values','status'}, 'response')
    _require(doc['status'] == 'ok', 'response is not ok')
    _closed(doc['meta'], META_KEYS, 'response metadata')
    for key in META_KEYS - {'exchange_timezone'}:
        _require(doc['meta'][key] == request[key], 'response identity mismatch: '+key)
    _require(doc['meta']['exchange_timezone'] == 'America/New_York', 'unsupported exchange timezone')
    rows = doc['values']
    _require(type(rows) is list and 1 <= len(rows) <= request['outputsize'], 'bounded nonempty rows required')
    previous = None
    for row in rows:
        _closed(row, {'datetime', *FIELDS}, 'response row')
        day = _day(row['datetime'])
        _require(start <= day <= end and (previous is None or previous < day), 'out-of-window, duplicate or unordered date')
        previous = day
        for field in FIELDS:
            value = row[field]
            _require(type(value) is str and 0 < len(value) <= 64 and
                     re.fullmatch(r'[0-9]+(?:\.[0-9]+)?',value), 'decimal string required: '+field)
            number = Decimal(value)
            _require(number >= 0 if field == 'volume' else number > 0, 'invalid value sign: '+field)
    return doc


def build_envelope(raw_response: bytes, request: dict, observed_at: str) -> bytes:
    """Return canonical envelope bytes; caller retains hash independently.

    Shape/identity/window checks are evidence hygiene, not market qualification.
    The original response bytes and numeric strings remain unchanged. No rule
    about rounding, adjusted volume, complete dates, or OHLC quality is inferred.
    """
    doc = _inspect(raw_response, request, observed_at)
    envelope = dict(schema_version=1, kind='offline_raw_response_evidence',
        status='OFFLINE_EVIDENCE_ONLY', provenance='CALLER_SUPPLIED_UNAUTHENTICATED',
        request=request, observed_at=observed_at, response_meta=doc['meta'],
        raw_response_base64=base64.b64encode(raw_response).decode('ascii'),
        raw_response_sha256=_sha(raw_response), records=doc['values'], claims=CLAIMS)
    encoded = _encode(envelope)
    _require(len(encoded) <= MAX_ENVELOPE_BYTES, 'envelope exceeds byte bound')
    return encoded


def verify_envelope(envelope: bytes, expected_sha256: str) -> dict:
    """Check exact anchor, raw bytes and derived metadata; does not confer trust."""
    _require(type(expected_sha256) is str and re.fullmatch(r'[0-9a-f]{64}',expected_sha256), 'external digest required')
    _require(type(envelope) is bytes and 0 < len(envelope) <= MAX_ENVELOPE_BYTES, 'bounded envelope required')
    _require(_sha(envelope) == expected_sha256, 'envelope anchor mismatch')
    doc = _load(envelope, MAX_ENVELOPE_BYTES)
    _closed(doc, set('schema_version kind status provenance request observed_at response_meta raw_response_base64 raw_response_sha256 records claims'.split()), 'envelope')
    _require(type(doc['raw_response_base64']) is str, 'base64 text required')
    try:
        raw = base64.b64decode(doc['raw_response_base64'], validate=True)
    except (ValueError, binascii.Error) as exc:
        raise EvidenceError('invalid raw response encoding') from exc
    rebuilt = build_envelope(raw, doc['request'], doc['observed_at'])
    _require(rebuilt == envelope, 'envelope is noncanonical or inconsistent')
    return {'raw_response':raw, 'request':doc['request'], 'observed_at':doc['observed_at'],
            'records':doc['records'], 'claims':doc['claims']}
