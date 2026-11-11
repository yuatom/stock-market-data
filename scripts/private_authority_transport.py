"""Unbound read-only metadata seam. Never an execution grant or object fetch.

The receipt pin must be independently approved and supplied privately by the
caller. This module is deliberately absent from production workflow wiring.
"""
from dataclasses import dataclass, field
import json
import re
import urllib.request
from scripts.private_authority_input import ROUTES, STORE_REPOSITORY, MAX_JSON_BYTES


class PrivateTransportError(RuntimeError):
    """Constant failure without credentials, private objects or upstream errors."""


@dataclass(frozen=True, repr=False)
class PrivateReference:
    descriptor_commit: str = field(repr=False)

    def __repr__(self):
        return '<PrivateReference>'

    @property
    def authorization_authenticated(self):
        return False

    @property
    def production_activation(self):
        return False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _need(value):
    if not value:
        raise ValueError()


def _pairs(items):
    result = {}
    for key, value in items:
        _need(key not in result)
        result[key] = value
    return result


def _constant(_):
    raise ValueError()


def resolve_private_reference(token, route, compute_sha, approved_descriptor_commit):
    """Verify visibility and ref against an independent private receipt.

No retries, redirects, configurable hosts, output, ref writes, object downloads
or execution. A successful lookup is not proof of user approval. The future
caller must still validate the receipt's origin, materialize exact objects,
apply load_authority, recheck remote heads and run all existing runtime gates.
"""
    try:
        _need(type(token) is str and re.fullmatch(r'[A-Za-z0-9_.-]{1,4096}', token))
        _need(type(route) is str and route in ROUTES)
        for value in (compute_sha, approved_descriptor_commit):
            _need(type(value) is str and re.fullmatch(r'[0-9a-f]{40}', value))
        base = 'https://api.github.com/repos/' + STORE_REPOSITORY
        ref = f'refs/heads/capture-authority/{route}/{compute_sha}'
        # No ambient proxy can receive the bearer credential. Normal TLS
        # certificate checks remain enabled; redirects are never followed.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

        def get(url):
            request = urllib.request.Request(url, method='GET', headers={
                'Authorization': 'Bearer ' + token,
                'Accept': 'application/vnd.github+json',
                'X-GitHub-Api-Version': '2022-11-28',
                'User-Agent': 'private-authority-selector'})
            with opener.open(request, timeout=30) as response:
                _need(response.status == 200 and response.geturl() == url)
                raw = response.read(MAX_JSON_BYTES + 1)
            _need(0 < len(raw) <= MAX_JSON_BYTES)
            value = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs, parse_constant=_constant)
            _need(type(value) is dict)
            return value

        metadata = get(base)
        _need(metadata.get('private') is True and metadata.get('full_name') == STORE_REPOSITORY)
        reference = get(base + '/git/ref/' + ref[5:])
        obj = reference.get('object')
        _need(reference.get('ref') == ref and type(obj) is dict
              and obj.get('type') == 'commit' and obj.get('sha') == approved_descriptor_commit)
        return PrivateReference(approved_descriptor_commit)
    except Exception:
        pass
    raise PrivateTransportError('PRIVATE_TRANSPORT_INVALID')
