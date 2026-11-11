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
