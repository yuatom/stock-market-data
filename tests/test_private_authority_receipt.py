"""Synthetic receipt protocol tests. No credentials, network, Git or runtime."""
import hashlib
import importlib
import json

import pytest


def module():
    return importlib.import_module('scripts.private_authority_receipt')


def fields():
    return dict(schema_version=1, kind='capture_authority_receipt',
                route='soxx_capture_only', compute_sha='1' * 40,
                authority_store_sha='2' * 40, descriptor_commit='3' * 40,
                nonce='4' * 64)


def wire(value=None):
    return (json.dumps(fields() if value is None else value, sort_keys=True,
                       separators=(',', ':')) + '\n').encode('ascii')


def test_canonical_encoding_and_domain_separation():
    m = module()
    raw = wire()
    assert m.canonical_receipt(fields()) == raw
    expected = hashlib.sha256(b'private-authority-receipt:v1\x00' + raw).hexdigest()
    assert m.receipt_commitment(raw) == expected
    assert m.receipt_commitment(raw) != hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize('route', ['soxx_capture_only', 'eleven_symbol_conditional_append'])
def test_verified_receipt_is_private_and_not_consent(route):
    m = module()
    data = fields(); data['route'] = route
    raw = wire(data)
    receipt = m.verify_receipt(raw, m.receipt_commitment(raw), route, '1' * 40)
    assert receipt.authority_store_sha == '2' * 40
    assert receipt.descriptor_commit == '3' * 40
    assert receipt.compute_sha == '1' * 40
    assert receipt.route == route
    assert receipt.nonce == '4' * 64
    assert receipt.authorization_authenticated is False
    assert receipt.production_activation is False
    assert repr(receipt) == str(receipt) == '<PrivateAuthorityReceipt>'


@pytest.mark.parametrize('key,value', [
    ('schema_version', True), ('schema_version', 2), ('kind', 'other'),
    ('route', 'other'), ('compute_sha', 'A' * 40), ('compute_sha', '1' * 39),
    ('authority_store_sha', '2' * 41), ('descriptor_commit', None),
    ('nonce', '4' * 63), ('nonce', 'F' * 64), ('nonce', 4),
    ('nonce', '0' * 64), ('route', []), ('extra', 'secret'),
])
def test_closed_schema_rejects_invalid_fields(key, value):
    m = module()
    data = fields(); data[key] = value
    for action in (lambda: m.canonical_receipt(data), lambda: m.validate_receipt(wire(data))):
        with pytest.raises(m.PrivateReceiptError) as err:
            action()
        assert str(err.value) == 'PRIVATE_RECEIPT_INVALID'
        assert err.value.__context__ is None
        assert err.value.__cause__ is None


@pytest.mark.parametrize('key', list(fields()))
def test_every_field_required(key):
    m = module(); data = fields(); del data[key]
    with pytest.raises(m.PrivateReceiptError):
        m.validate_receipt(wire(data))


@pytest.mark.parametrize('raw', [
    b'', b'{}', b'[]\n', b'null\n', b'NaN\n', b'\xff', b' ' * 2049,
    wire().rstrip(b'\n'), wire() + b'\n', wire().replace(b':', b': ', 1),
    wire().replace(b'"schema_version":1', b'"schema_version":1,"schema_version":1'),
    wire().replace(b'"schema_version":1', b'"schema_version":NaN'),
    wire().decode(), bytearray(wire()),
])
def test_only_bounded_canonical_bytes_accepted(raw):
    m = module()
    for action in (lambda: m.validate_receipt(raw), lambda: m.receipt_commitment(raw)):
        with pytest.raises(m.PrivateReceiptError) as err:
            action()
        assert str(err.value) == 'PRIVATE_RECEIPT_INVALID'
        assert err.value.__context__ is None


@pytest.mark.parametrize('key,value', [
    ('route', 'eleven_symbol_conditional_append'), ('compute_sha', 'a' * 40),
    ('authority_store_sha', 'b' * 40), ('descriptor_commit', 'c' * 40),
    ('nonce', 'd' * 64),
])
def test_commitment_binds_all_private_fields(key, value):
    m = module(); raw = wire(); data = fields(); data[key] = value
    with pytest.raises(m.PrivateReceiptError):
        m.verify_receipt(wire(data), m.receipt_commitment(raw), data['route'], data['compute_sha'])


@pytest.mark.parametrize('digest,route,compute', [
    ('0' * 64, 'soxx_capture_only', '1' * 40),
    ('G' * 64, 'soxx_capture_only', '1' * 40),
    (None, 'soxx_capture_only', '1' * 40),
    ('valid', 'eleven_symbol_conditional_append', '1' * 40),
    ('valid', 'soxx_capture_only', 'a' * 40),
    ('valid', [], '1' * 40),
])
def test_untrusted_commitment_and_context_fail_closed(digest, route, compute):
    m = module(); raw = wire()
    if digest == 'valid':
        digest = m.receipt_commitment(raw)
    with pytest.raises(m.PrivateReceiptError) as err:
        m.verify_receipt(raw, digest, route, compute)
    assert str(err.value) == 'PRIVATE_RECEIPT_INVALID'
    assert err.value.__context__ is None


def test_repeated_commitment_is_linkable_but_fresh_nonce_changes_it():
    m = module(); raw = wire(); other = fields(); other['nonce'] = '5' * 64
    assert m.receipt_commitment(raw) == m.receipt_commitment(raw)
    assert m.receipt_commitment(raw) != m.receipt_commitment(wire(other))
