"""Unbound quiet private sidecar delivery; never consent or activation.

Only canonical bytes matching an independently approved public commitment are
returned. No execution, subprocess, writes, redirects, retries or ambient proxy.
Contents type=file is an API assertion, not a proof of literal Git mode: trusted
creation/readback or a later literal tree reader must ensure a normal file.
"""
import base64
import hashlib
import hmac
import json
import re
import urllib.parse
import urllib.request
from scripts.private_authority_receipt import ROUTES, MAX_RECEIPT_BYTES, verify_receipt

STORE_REPOSITORY = 'yuatom/stock-market-data-store'
RECEIPT_PATH = 'capture-authority/receipt.json'
MAX_RESPONSE_BYTES = 65536
MAX_BASE64_BYTES = 4096


class PrivateReceiptTransportError(RuntimeError):
    """Constant failure without credentials, private bytes or upstream context."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _need(condition):
    if not condition:
        raise ValueError()


def _hex(value, length):
    _need(type(value) is str and re.fullmatch('[0-9a-f]{' + str(length) + '}', value))


def _pairs(items):
    value = {}
    for key, entry in items:
        _need(key not in value)
        value[key] = entry
    return value


def _constant(_):
    raise ValueError()


def fetch_private_receipt(token, route, compute_sha, expected_commitment):
    """Read separate private receipt sidecar and bind to trusted dispatch inputs.

The trusted caller establishes owner/manual/main/first-attempt identity and
explicit approved creation/readback independently. Expected commitment must not
come from this sidecar. Returned bytes remain private; never log or publish them.
This function neither authenticates approval nor checks durable replay gates.
"""
    try:
        _need(type(token) is str and re.fullmatch(r'[A-Za-z0-9_.-]{1,4096}', token))
        _need(type(route) is str and route in ROUTES)
        _hex(compute_sha, 40)
        _hex(expected_commitment, 64)
        base = 'https://api.github.com/repos/' + STORE_REPOSITORY
        ref = f'refs/capture-receipts/{route}/{compute_sha}'
        url = base + '/contents/' + RECEIPT_PATH + '?ref=' + urllib.parse.quote(ref, safe='')
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

        def get(target):
            request = urllib.request.Request(target, method='GET', headers={
                'Authorization': 'Bearer ' + token,
                'Accept': 'application/vnd.github+json',
                'X-GitHub-Api-Version': '2022-11-28',
                'User-Agent': 'private-authority-receipt-reader'})
            with opener.open(request, timeout=30) as response:
                _need(response.status == 200 and response.geturl() == target)
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            _need(0 < len(raw) <= MAX_RESPONSE_BYTES)
            value = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs, parse_constant=_constant)
            _need(type(value) is dict)
            return value

        metadata = get(base)
        _need(metadata.get('private') is True and metadata.get('full_name') == STORE_REPOSITORY)
        entry = get(url)
        _need(entry.get('type') == 'file' and entry.get('path') == RECEIPT_PATH
              and entry.get('encoding') == 'base64')
        _need('target' not in entry and 'submodule_git_url' not in entry)
        size = entry.get('size')
        _need(type(size) is int and 0 < size <= MAX_RECEIPT_BYTES)
        sha = entry.get('sha')
        _hex(sha, 40)
        content = entry.get('content')
        _need(type(content) is str and 0 < len(content) <= MAX_BASE64_BYTES)
        # GitHub wraps base64 with LF. Reject other whitespace and noncanonical
        # padding/unused bits rather than accepting the decoder's loose forms.
        encoded = content.replace('\n', '').encode('ascii')
        raw = base64.b64decode(encoded, validate=True)
        _need(base64.b64encode(raw) == encoded)
        _need(len(raw) == size and len(raw) <= MAX_RECEIPT_BYTES)
        actual = hashlib.sha1(b'blob ' + str(len(raw)).encode('ascii') + b'\0' + raw).hexdigest()
        _need(hmac.compare_digest(actual, sha))
        verify_receipt(raw, expected_commitment, route, compute_sha)
        return raw
    except Exception:
        pass
    raise PrivateReceiptTransportError('PRIVATE_RECEIPT_TRANSPORT_INVALID')
