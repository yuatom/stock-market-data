"""Synthetic HTTPS responses only; no production requests or credentials."""
import io
import json
from unittest.mock import patch
import pytest
from scripts import private_authority_input as selection

COMPUTE = 'c' * 40
PIN = 'd' * 40
ROUTE = 'soxx_capture_only'
REF = f'refs/heads/capture-authority/{ROUTE}/{COMPUTE}'
BASE = 'https://api.github.com/repos/' + selection.STORE_REPOSITORY


def transport():
    from scripts import private_authority_transport
    return private_authority_transport


class Response(io.BytesIO):
    status = 200
    def __init__(self, value, url):
        super().__init__(value if isinstance(value, bytes) else json.dumps(value).encode())
        self.url = url
    def geturl(self):
        return self.url


def resolve(metadata=None, reference=None, failure=None):
    module = transport()
    values = [metadata if metadata is not None else {'private': True, 'full_name': selection.STORE_REPOSITORY},
              reference if reference is not None else {'ref': REF, 'object': {'type': 'commit', 'sha': PIN}}]
    requests = []
    def open_request(request, timeout):
        requests.append(request)
        if failure:
            raise failure
        return Response(values.pop(0), request.full_url)
    with patch.object(module.urllib.request, 'build_opener') as build:
        build.return_value.open.side_effect = open_request
        result = module.resolve_private_reference('synthetic-token', ROUTE, COMPUTE, PIN)
    return result, requests


def test_verified_metadata_uses_exact_requests_and_keeps_results_private(capsys):
    result, requests = resolve()
    assert result.descriptor_commit == PIN
    assert result.authorization_authenticated is False
    assert result.production_activation is False
    assert PIN not in repr(result)
    assert [r.full_url for r in requests] == [BASE, BASE + '/git/ref/' + REF[5:]]
    assert all(r.get_header('Authorization') == 'Bearer synthetic-token' for r in requests)
    assert capsys.readouterr() == ('', '')


@pytest.mark.parametrize('metadata', [ {'private': False, 'full_name': selection.STORE_REPOSITORY},
    {'private': 1, 'full_name': selection.STORE_REPOSITORY}, {'private': True, 'full_name': 'other/repo'},
    b'{"private":true,"private":false}', b'[]', b'x' * 65537])
def test_bad_repository_metadata_rejected(metadata):
    with pytest.raises(transport().PrivateTransportError, match='^PRIVATE_TRANSPORT_INVALID$'):
        resolve(metadata=metadata)


@pytest.mark.parametrize('reference', [ {'ref': REF, 'object': {'type': 'tag', 'sha': PIN}},
    {'ref': REF, 'object': {'type': 'commit', 'sha': 'a' * 40}},
    {'ref': REF + '/other', 'object': {'type': 'commit', 'sha': PIN}}, {}])
def test_ref_cannot_substitute_receipt_pin(reference):
    with pytest.raises(transport().PrivateTransportError):
        resolve(reference=reference)


def test_failure_has_no_private_exception_context(capsys):
    with pytest.raises(transport().PrivateTransportError) as caught:
        resolve(failure=RuntimeError('private-token-and-pin'))
    assert caught.value.__context__ is None
    assert 'private-token-and-pin' not in str(caught.value)
    assert capsys.readouterr() == ('', '')


@pytest.mark.parametrize('args', [('bad\nsecret', ROUTE, COMPUTE, PIN), ('secret', '../route', COMPUTE, PIN),
    ('secret', ROUTE, 'x' * 40, PIN), ('secret', ROUTE, COMPUTE, '')])
def test_invalid_input_never_opens_network(args):
    module = transport()
    with patch.object(module.urllib.request, 'build_opener') as build:
        with pytest.raises(module.PrivateTransportError):
            module.resolve_private_reference(*args)
        build.assert_not_called()


def test_redirect_handler_refuses_every_redirect():
    module = transport()
    assert module._NoRedirect().redirect_request(None, None, 302, 'redirect', {}, 'https://elsewhere.invalid') is None


def test_production_opener_has_no_proxy_and_no_redirect():
    module = transport()
    with patch.object(module.urllib.request, 'build_opener') as build:
        build.return_value.open.side_effect = RuntimeError('synthetic')
        with pytest.raises(module.PrivateTransportError):
            module.resolve_private_reference('secret', ROUTE, COMPUTE, PIN)
        handlers = build.call_args.args
        assert any(isinstance(h, module.urllib.request.ProxyHandler) and h.proxies == {} for h in handlers)
        assert any(isinstance(h, module._NoRedirect) for h in handlers)
        assert build.return_value.open.call_count == 1
        request = build.return_value.open.call_args.args[0]
        assert request.get_method() == 'GET'
        assert build.return_value.open.call_args.kwargs == {'timeout': 30}


@pytest.mark.parametrize('status', [301, 302, 303, 307, 308])
def test_http_redirect_processing_never_reissues_authorization(status):
    module = transport()
    from urllib.error import HTTPError
    from urllib.response import addinfourl
    from email.message import Message
    calls = []
    class SyntheticHTTPS(module.urllib.request.HTTPSHandler):
        def https_open(self, request):
            calls.append(request)
            headers = Message()
            headers['Location'] = 'https://elsewhere.invalid'
            response = addinfourl(io.BytesIO(b''), headers, request.full_url, status)
            response.msg = 'redirect'
            return response
    opener = module.urllib.request.build_opener(module.urllib.request.ProxyHandler({}),
                                               module._NoRedirect(), SyntheticHTTPS())
    request = module.urllib.request.Request(BASE, headers={'Authorization': 'Bearer synthetic-token'})
    with pytest.raises(HTTPError):
        opener.open(request)
    assert len(calls) == 1
    assert calls[0].full_url == BASE



@pytest.mark.parametrize('raw', [b'', b'\xff', b'{', b'{"x":NaN}', b'{"x":Infinity}', b'{"x":{"k":1,"k":2}}', b'x' * 65537])
@pytest.mark.parametrize('position', [0, 1])
def test_bad_response_bytes_stop_quietly_without_retry(raw, position, capsys):
    module = transport()
    values = [{'private': True, 'full_name': selection.STORE_REPOSITORY}, raw] if position else [raw]
    def open_request(request, timeout):
        return Response(values.pop(0), request.full_url)
    with patch.object(module.urllib.request, 'build_opener') as build:
        build.return_value.open.side_effect = open_request
        with pytest.raises(module.PrivateTransportError) as caught:
            module.resolve_private_reference('secret', ROUTE, COMPUTE, PIN)
        assert build.return_value.open.call_count == position + 1
        assert caught.value.__context__ is None
    assert capsys.readouterr() == ('', '')


@pytest.mark.parametrize('status,url', [(403, BASE), (200, 'https://elsewhere.invalid')])
def test_status_and_response_destination_rejected(status, url):
    module = transport()
    response = Response({'private': True, 'full_name': selection.STORE_REPOSITORY}, url)
    response.status = status
    with patch.object(module.urllib.request, 'build_opener') as build:
        build.return_value.open.return_value = response
        with pytest.raises(module.PrivateTransportError):
            module.resolve_private_reference('secret', ROUTE, COMPUTE, PIN)
        assert build.return_value.open.call_count == 1


def test_exact_byte_limit_is_accepted_but_larger_body_rejected():
    valid = json.dumps({'private': True, 'full_name': selection.STORE_REPOSITORY}).encode()
    exact = valid + b' ' * (65536 - len(valid))
    assert resolve(metadata=exact)[0].descriptor_commit == PIN
    with pytest.raises(transport().PrivateTransportError):
        resolve(metadata=exact + b' ')
