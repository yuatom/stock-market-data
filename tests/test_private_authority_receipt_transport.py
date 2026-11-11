"""Offline synthetic HTTP boundary tests; no production credentials/data."""
import base64
import hashlib
import importlib
import io
import json
from unittest.mock import patch
import pytest
from scripts.private_authority_receipt import canonical_receipt, receipt_commitment

ROUTE = 'soxx_capture_only'
COMPUTE = 'c' * 40
BASE = 'https://api.github.com/repos/yuatom/stock-market-data-store'
PATH = 'capture-authority/receipt.json'
URL = BASE + '/contents/' + PATH + '?ref=refs%2Fheads%2Fcapture-receipts%2F' + ROUTE + '%2F' + COMPUTE
RAW = canonical_receipt(dict(schema_version=1, kind='capture_authority_receipt', route=ROUTE,
    compute_sha=COMPUTE, authority_store_sha='a' * 40, descriptor_commit='b' * 40, nonce='d' * 64))
DIGEST = receipt_commitment(RAW)


def module():
    return importlib.import_module('scripts.private_authority_receipt_transport')


def contents():
    return dict(type='file', path=PATH, encoding='base64', size=len(RAW),
        sha=hashlib.sha1(b'blob ' + str(len(RAW)).encode() + b'\0' + RAW).hexdigest(),
        content=base64.b64encode(RAW).decode() + '\n')


class Response(io.BytesIO):
    def __init__(self, value, url, status=200):
        super().__init__(value if isinstance(value, bytes) else json.dumps(value).encode())
        self.url, self.status = url, status
    def geturl(self):
        return self.url


def run(metadata=None, entry=None, args=None, status=200, url=None, failure=None):
    m = module(); calls = []
    values = [metadata if metadata is not None else dict(private=True, full_name='yuatom/stock-market-data-store'),
              contents() if entry is None else entry]
    def open_request(request, timeout):
        calls.append(request)
        assert timeout == 30
        if failure:
            raise failure
        return Response(values.pop(0), url or request.full_url, status)
    with patch.object(m.urllib.request, 'build_opener') as build:
        build.return_value.open.side_effect = open_request
        result = m.fetch_private_receipt(*(args or ('synthetic-token', ROUTE, COMPUTE, DIGEST)))
    return result, calls


def test_exact_quiet_requests_and_commitment_bound_private_bytes(capsys):
    raw, calls = run()
    assert raw == RAW
    assert [r.full_url for r in calls] == [BASE, URL]
    assert all(r.get_method() == 'GET' for r in calls)
    assert all(r.get_header('Authorization') == 'Bearer synthetic-token' for r in calls)
    assert capsys.readouterr() == ('', '')


@pytest.mark.parametrize('key,value', [
    ('type','symlink'), ('type','dir'), ('path','other'), ('encoding','none'),
    ('size',True), ('size',0), ('size',2049), ('size',1), ('sha','a'*40), ('sha','G'*40),
    ('content','%%%'), ('content','A'*4097), ('content',''), ('content',None),
    ('target','private'), ('submodule_git_url','private')])
def test_rejects_invalid_file_metadata_and_content(key,value):
    item=contents(); item[key]=value
    with pytest.raises(module().PrivateReceiptTransportError):
        run(entry=item)


@pytest.mark.parametrize('key', ['type','path','encoding','size','sha','content'])
def test_required_metadata(key):
    item=contents(); del item[key]
    with pytest.raises(module().PrivateReceiptTransportError):
        run(entry=item)


@pytest.mark.parametrize('metadata', [dict(private=False,full_name='yuatom/stock-market-data-store'),
    dict(private=1,full_name='yuatom/stock-market-data-store'), dict(private=True,full_name='other/repo'),
    b'{"private":true,"private":false}', b'NaN', b'[]', b'x'*65537])
def test_private_exact_repository_required(metadata):
    with pytest.raises(module().PrivateReceiptTransportError):
        run(metadata=metadata)


@pytest.mark.parametrize('entry', [b'{}',b'[]',b'null',b'{"a":1,"a":2}',b'NaN',b'x'*65537])
def test_bad_response_json(entry):
    with pytest.raises(module().PrivateReceiptTransportError):
        run(entry=entry)


@pytest.mark.parametrize('status', [301,302,303,307,308,401,404,500])
def test_http_failure_rejected(status):
    with pytest.raises(module().PrivateReceiptTransportError):
        run(status=status)


def test_response_url_must_match_requested_url():
    with pytest.raises(module().PrivateReceiptTransportError):
        run(url='https://other.invalid/')


@pytest.mark.parametrize('args', [('bad\nsecret',ROUTE,COMPUTE,DIGEST),
    ('secret','../route',COMPUTE,DIGEST), ('secret',ROUTE,'x'*40,DIGEST),
    ('secret',ROUTE,COMPUTE,'x'*64)])
def test_invalid_inputs_make_no_requests(args):
    m=module()
    with patch.object(m.urllib.request,'build_opener') as build:
        with pytest.raises(m.PrivateReceiptTransportError):
            m.fetch_private_receipt(*args)
        build.assert_not_called()


def test_valid_blob_cannot_substitute_approved_receipt():
    with pytest.raises(module().PrivateReceiptTransportError):
        run(args=('secret',ROUTE,COMPUTE,'0'*64))


def test_valid_blob_cannot_substitute_reviewed_compute():
    with pytest.raises(module().PrivateReceiptTransportError):
        run(args=('secret',ROUTE,'e'*40,DIGEST))


def test_valid_blob_cannot_substitute_workflow_route():
    with pytest.raises(module().PrivateReceiptTransportError):
        run(args=('secret','eleven_symbol_conditional_append',COMPUTE,DIGEST))


def test_upstream_secrets_not_in_exception_or_output(capsys):
    with pytest.raises(module().PrivateReceiptTransportError) as err:
        run(failure=RuntimeError('private-token-pin'))
    assert str(err.value)=='PRIVATE_RECEIPT_TRANSPORT_INVALID'
    assert err.value.__context__ is None
    assert err.value.__cause__ is None
    assert capsys.readouterr()==('','')


def test_transport_disables_proxy_redirect_and_retry():
    m=module()
    with patch.object(m.urllib.request,'build_opener') as build:
        build.return_value.open.side_effect=RuntimeError('offline')
        with pytest.raises(m.PrivateReceiptTransportError):
            m.fetch_private_receipt('secret',ROUTE,COMPUTE,DIGEST)
        assert build.return_value.open.call_count==1
        assert any(isinstance(h,m.urllib.request.ProxyHandler) and h.proxies=={} for h in build.call_args.args)
        assert any(isinstance(h,m._NoRedirect) for h in build.call_args.args)
    assert m._NoRedirect().redirect_request(None,None,302,'redirect',{},'https://other.invalid') is None


@pytest.mark.parametrize('status', [301,302,303,307,308])
def test_real_opener_does_not_forward_authorization_on_redirect(status):
    m=module()
    from urllib.error import HTTPError
    from urllib.response import addinfourl
    from email.message import Message
    calls=[]
    class SyntheticHTTPS(m.urllib.request.HTTPSHandler):
        def https_open(self, request):
            calls.append(request)
            headers=Message(); headers['Location']='https://elsewhere.invalid'
            response=addinfourl(io.BytesIO(b''),headers,request.full_url,status)
            response.msg='redirect'
            return response
    opener=m.urllib.request.build_opener(m.urllib.request.ProxyHandler({}),m._NoRedirect(),SyntheticHTTPS())
    with pytest.raises(HTTPError):
        opener.open(m.urllib.request.Request(BASE,headers={'Authorization':'Bearer synthetic-token'}))
    assert len(calls)==1
    assert calls[0].full_url==BASE


def test_bad_privacy_stops_before_receipt_request():
    m=module()
    with patch.object(m.urllib.request,'build_opener') as build:
        build.return_value.open.return_value=Response(dict(private=False,full_name='yuatom/stock-market-data-store'),BASE)
        with pytest.raises(m.PrivateReceiptTransportError):
            m.fetch_private_receipt('secret',ROUTE,COMPUTE,DIGEST)
        assert build.return_value.open.call_count==1


def test_exact_response_bound_and_one_byte_over():
    raw=json.dumps(dict(private=True,full_name='yuatom/stock-market-data-store')).encode()
    exact=raw+b' '*(65536-len(raw))
    assert run(metadata=exact)[0]==RAW
    with pytest.raises(module().PrivateReceiptTransportError):
        run(metadata=exact+b' ')


def test_noncanonical_receipt_with_correct_blob_hash_still_rejected():
    altered=RAW+b'\n'; item=contents()
    item.update(content=base64.b64encode(altered).decode(),size=len(altered),
        sha=hashlib.sha1(b'blob '+str(len(altered)).encode()+b'\0'+altered).hexdigest())
    with pytest.raises(module().PrivateReceiptTransportError):
        run(entry=item)
