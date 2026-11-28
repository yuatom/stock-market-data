"""Pure, unbound private receipt commitment candidate; never an execution grant.

No CLI, randomness generation, filesystem, network, workflow or runtime wiring.
The trusted creator must supply a fresh cryptographically random 32-byte nonce.
Validation checks representation, not entropy or origin. Caller provenance,
explicit approval and every existing execution/replay gate remain mandatory.
"""
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import re

DOMAIN = b'private-authority-receipt:v1\x00'
MAX_RECEIPT_BYTES = 2048
ROUTES = frozenset({'soxx_capture_only', 'eleven_symbol_conditional_append'})
_KEYS = frozenset({'schema_version', 'kind', 'route', 'compute_sha',
                   'authority_store_sha', 'descriptor_commit', 'nonce'})


class PrivateReceiptError(RuntimeError):
    """Value-free failure, without underlying exception context."""


@dataclass(frozen=True, repr=False)
class PrivateAuthorityReceipt:
    route: str = field(repr=False)
    compute_sha: str = field(repr=False)
    authority_store_sha: str = field(repr=False)
    descriptor_commit: str = field(repr=False)
    nonce: str = field(repr=False)

    def __repr__(self):
        return '<PrivateAuthorityReceipt>'

    @property
    def authorization_authenticated(self):
        return False

    @property
    def production_activation(self):
        return False


def _need(condition):
    if not condition:
        raise ValueError()


def _hex(value, size):
    _need(type(value) is str and re.fullmatch('[0-9a-f]{' + str(size) + '}', value))


def _validate(value):
    _need(type(value) is dict and set(value) == _KEYS)
    _need(type(value['schema_version']) is int and value['schema_version'] == 1)
    _need(type(value['kind']) is str and value['kind'] == 'capture_authority_receipt')
    _need(type(value['route']) is str and value['route'] in ROUTES)
    for name in ('compute_sha', 'authority_store_sha', 'descriptor_commit'):
        _hex(value[name], 40)
    _hex(value['nonce'], 64)
    _need(value['nonce'] != '0' * 64)  # Reject the obvious unset sentinel, not an entropy test.


def _encode(value):
    _validate(value)
    return (json.dumps(value, sort_keys=True, separators=(',', ':'),
                       ensure_ascii=True, allow_nan=False) + '\n').encode('ascii')


def _pairs(items):
    value = {}
    for key, entry in items:
        _need(key not in value)
        value[key] = entry
    return value


def _constant(_):
    raise ValueError()


def _parse(raw):
    _need(type(raw) is bytes and 0 < len(raw) <= MAX_RECEIPT_BYTES)
    value = json.loads(raw.decode('ascii'), object_pairs_hook=_pairs, parse_constant=_constant)
    _need(_encode(value) == raw)
    return value


def _receipt(value):
    return PrivateAuthorityReceipt(*(value[key] for key in
        ('route', 'compute_sha', 'authority_store_sha', 'descriptor_commit', 'nonce')))


def canonical_receipt(value):
    """Encode a closed receipt mapping. Returned bytes are PRIVATE, not loggable."""
    try:
        return _encode(value)
    except Exception:
        pass
    raise PrivateReceiptError('PRIVATE_RECEIPT_INVALID')


def validate_receipt(raw):
    """Validate bounded canonical bytes; does not prove approval or nonce entropy."""
    try:
        return _receipt(_parse(raw))
    except Exception:
        pass
    raise PrivateReceiptError('PRIVATE_RECEIPT_INVALID')


def receipt_commitment(raw):
    """Return prospective PUBLIC digest; publishing it still requires approval."""
    try:
        _parse(raw)
        return hashlib.sha256(DOMAIN + raw).hexdigest()
    except Exception:
        pass
    raise PrivateReceiptError('PRIVATE_RECEIPT_INVALID')


def verify_receipt(raw, expected_commitment, expected_route, expected_compute_sha):
    """Check digest plus trusted workflow route and independently reviewed source.

This proves byte binding only, not the provenance or authority of the expected
commitment. Do not take these expectations from the receipt itself or infer
consent from a digest, token, mutable reference, or this result.
"""
    try:
        _hex(expected_commitment, 64)
        _need(type(expected_route) is str and expected_route in ROUTES)
        _hex(expected_compute_sha, 40)
        value = _parse(raw)
        actual = hashlib.sha256(DOMAIN + raw).hexdigest()
        _need(hmac.compare_digest(actual, expected_commitment))
        _need(value['route'] == expected_route and value['compute_sha'] == expected_compute_sha)
        return _receipt(value)
    except Exception:
        pass
    raise PrivateReceiptError('PRIVATE_RECEIPT_INVALID')


# Separate private manual-repair receipt lane. Do not add it to legacy ROUTES:
# the deployed transport and the two old execution owners do not authorize it.
from datetime import date,datetime,timedelta,timezone

_MANUAL_ROUTE='soxx_manual_repair'
_MANUAL_DOMAIN=b'private-soxx-manual-repair-receipt:v1\x00'
_STORE='yuatom/stock-market-data-store'
_MANUAL_KEYS=frozenset({'schema_version','kind','route','compute_sha','authority_store_sha',
    'descriptor_commit','origin','symbol','start_date','end_date','native_calendar_sha256',
    'lookahead_sha256','valid_from','valid_until','nonce'})
_MANUAL_ORIGIN_KEYS=frozenset({'repository','repository_id','owner_id'})

@dataclass(frozen=True,repr=False)
class ManualReceiptOrigin:
    """Independent audit/issuer expectations; never derive from supplied raw."""
    compute_sha:str
    authority_store_sha:str
    descriptor_commit:str
    repository_id:int
    owner_id:int

@dataclass(frozen=True,repr=False)
class ManualRepairReceipt:
    compute_sha:str
    authority_store_sha:str
    descriptor_commit:str
    start_date:str
    end_date:str
    native_calendar_sha256:str
    lookahead_sha256:str
    valid_from:datetime
    valid_until:datetime

    def __repr__(self):return '<ManualRepairReceipt>'
    @property
    def authorization_authenticated(self):return False
    @property
    def production_activation(self):return False

def _manual_day(raw):
    _need(type(raw) is str and re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}',raw))
    value=date.fromisoformat(raw)
    _need(value.isoformat()==raw)
    return value

def _manual_stamp(raw):
    # Authorization windows require a known offset. Reject RFC 3339's
    # unknown-offset spelling (-00:00), rather than normalizing it to UTC.
    _need(type(raw) is str and not raw.endswith('-00:00') and re.fullmatch(
        r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}'
        r'(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])',raw))
    parsed=datetime.fromisoformat(raw.replace('Z','+00:00'))
    _need(parsed.tzinfo is not None and parsed.utcoffset() is not None)
    return parsed.astimezone(timezone.utc)

def _manual_encode(value):
    _need(type(value) is dict and set(value)==_MANUAL_KEYS)
    _need(type(value['schema_version']) is int and value['schema_version']==1 and
          value['kind']=='manual_soxx_repair_receipt' and value['route']==_MANUAL_ROUTE and value['symbol']=='SOXX')
    for key in ('compute_sha','authority_store_sha','descriptor_commit'):_hex(value[key],40)
    for key in ('native_calendar_sha256','lookahead_sha256','nonce'):_hex(value[key],64)
    _need(value['nonce']!='0'*64)
    origin=value['origin']
    _need(type(origin) is dict and set(origin)==_MANUAL_ORIGIN_KEYS and origin['repository']==_STORE)
    _need(all(type(origin[k]) is int and origin[k]>0 for k in ('repository_id','owner_id')))
    _need(timedelta(0)<=_manual_day(value['end_date'])-_manual_day(value['start_date'])<=timedelta(days=62))
    _need(timedelta(0)<_manual_stamp(value['valid_until'])-_manual_stamp(value['valid_from'])<=timedelta(hours=24))
    return (json.dumps(value,sort_keys=True,separators=(',',':'),
                       ensure_ascii=True,allow_nan=False)+'\n').encode('ascii')

def _manual_parse(raw):
    _need(type(raw) is bytes and 0<len(raw)<=MAX_RECEIPT_BYTES)
    decoded=json.loads(raw.decode('ascii'),object_pairs_hook=_pairs,parse_constant=_constant)
    _need(_manual_encode(decoded)==raw)
    return decoded

def canonical_manual_repair_receipt(value):
    """Encoding only, not an issuer. Private bytes are not public inputs."""
    try:return _manual_encode(value)
    except Exception:pass
    raise PrivateReceiptError('MANUAL_PRIVATE_RECEIPT_INVALID')

def manual_repair_commitment(raw):
    try:
        _manual_parse(raw)
        return hashlib.sha256(_MANUAL_DOMAIN+raw).hexdigest()
    except Exception:pass
    raise PrivateReceiptError('MANUAL_PRIVATE_RECEIPT_INVALID')

def verify_manual_repair_receipt(raw,commitment,expected,*,now):
    """Byte-bind separately expected origin and issue window, never grant work."""
    try:
        _hex(commitment,64)
        _need(type(expected) is ManualReceiptOrigin and isinstance(now,datetime) and
              now.tzinfo is not None and now.utcoffset() is not None)
        _need(all(type(getattr(expected,k)) is int and getattr(expected,k)>0 for k in ('repository_id','owner_id')))
        d=_manual_parse(raw)
        _need(hmac.compare_digest(hashlib.sha256(_MANUAL_DOMAIN+raw).hexdigest(),commitment))
        for k in ('compute_sha','authority_store_sha','descriptor_commit'):
            _hex(getattr(expected,k),40);_need(d[k]==getattr(expected,k))
        _need(all(d['origin'][k]==getattr(expected,k) for k in ('repository_id','owner_id')))
        start,until=_manual_stamp(d['valid_from']),_manual_stamp(d['valid_until'])
        _need(start<=now<until)
        return ManualRepairReceipt(*(d[k] for k in ('compute_sha','authority_store_sha','descriptor_commit',
            'start_date','end_date','native_calendar_sha256','lookahead_sha256')),start,until)
    except Exception:pass
    raise PrivateReceiptError('MANUAL_PRIVATE_RECEIPT_INVALID')
