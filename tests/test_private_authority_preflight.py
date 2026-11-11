"""Preflight only, never provider execution or a receipt authenticity decision."""
from unittest.mock import patch
import pytest

C='c'*40; S='a'*40; D='d'*40


def module():
    from scripts import private_authority_preflight
    return private_authority_preflight


def environment(route):
    workflow = 'soxx-capture-only.yml' if route == 'soxx_capture_only' else 'eleven-symbol-append.yml'
    return dict(GITHUB_ACTIONS='true',GITHUB_EVENT_NAME='workflow_dispatch',GITHUB_REF='refs/heads/main',
        GITHUB_SHA=C,GITHUB_RUN_ATTEMPT='1',GITHUB_RUN_ID='123',GITHUB_REPOSITORY='yuatom/stock-market-data',
        GITHUB_ACTOR='yuatom',GITHUB_TRIGGERING_ACTOR='yuatom',
        GITHUB_WORKFLOW_REF='yuatom/stock-market-data/.github/workflows/'+workflow+'@refs/heads/main')


@pytest.mark.parametrize('route',['soxx_capture_only','eleven_symbol_conditional_append'])
def test_preflight_binds_surface_before_private_access(route,tmp_path):
    m=module(); env=environment(route)
    with patch.object(m,'materialize_store') as materialize:
        selection=object();materialize.return_value=selection
        result=m.prepare_private_authority(token='secret',route=route,compute_sha=C,
            expected_store_head=S,descriptor_commit=D,destination=tmp_path/'store',env=env)
        assert result is selection
        materialize.assert_called_once_with('secret',route,C,S,D,tmp_path/'store')


@pytest.mark.parametrize('key,value', [('GITHUB_EVENT_NAME','push'),('GITHUB_RUN_ATTEMPT','2'),
    ('GITHUB_ACTOR','someone'),('GITHUB_TRIGGERING_ACTOR','someone'),('GITHUB_SHA','b'*40),
    ('GITHUB_REF','refs/heads/branch'),('GITHUB_REPOSITORY','other/repo'),('GITHUB_RUN_ID','0'),
    ('GITHUB_WORKFLOW_REF','yuatom/stock-market-data/.github/workflows/eleven-symbol-append.yml@refs/heads/main')])
def test_invalid_surface_never_reads_private_store(key,value,tmp_path):
    m=module();env=environment('soxx_capture_only');env[key]=value
    with patch.object(m,'materialize_store') as materialize:
        with pytest.raises(m.PrivatePreflightError) as caught:
            m.prepare_private_authority(token='secret',route='soxx_capture_only',compute_sha=C,
                expected_store_head=S,descriptor_commit=D,destination=tmp_path/'store',env=env)
        assert caught.value.__context__ is None
        materialize.assert_not_called()


def private_receipt():
    from scripts.private_authority_receipt import canonical_receipt,receipt_commitment
    raw=canonical_receipt(dict(schema_version=1,kind='capture_authority_receipt',route='soxx_capture_only',
        compute_sha=C,authority_store_sha=S,descriptor_commit=D,nonce='1'*64))
    return raw,receipt_commitment(raw)


def test_committed_preflight_binds_receipt_before_materialization(tmp_path):
    m=module();raw,commitment=private_receipt()
    with patch.object(m,'materialize_store') as materialize:
        result=m.prepare_committed_authority(token='secret',route='soxx_capture_only',compute_sha=C,
            receipt_raw=raw,expected_commitment=commitment,destination=tmp_path/'store',env=environment('soxx_capture_only'))
        assert result is materialize.return_value
        materialize.assert_called_once_with('secret','soxx_capture_only',C,S,D,tmp_path/'store')


@pytest.mark.parametrize('change',['commitment','route','compute','raw'])
def test_bad_commitment_prevents_private_materialization(tmp_path,change):
    m=module();raw,commitment=private_receipt()
    args=dict(token='secret',route='soxx_capture_only',compute_sha=C,receipt_raw=raw,
        expected_commitment=commitment,destination=tmp_path/'store',env=environment('soxx_capture_only'))
    if change == 'commitment':args['expected_commitment']='0'*64
    if change == 'route':args['route']='eleven_symbol_conditional_append'
    if change == 'compute':args['compute_sha']='e'*40
    if change == 'raw':args['receipt_raw']=raw.replace(S.encode(),('f'*40).encode())
    with patch.object(m,'materialize_store') as materialize:
        with pytest.raises(m.PrivatePreflightError) as caught:
            m.prepare_committed_authority(**args)
        assert caught.value.__context__ is None
        materialize.assert_not_called()


def test_hosted_receipt_read_follows_identity_check(tmp_path):
    m=module();raw,commitment=private_receipt()
    with patch.object(m,'fetch_private_receipt',return_value=raw) as fetch, patch.object(m,'materialize_store') as materialize:
        result=m.prepare_hosted_authority(token='secret',route='soxx_capture_only',compute_sha=C,
            expected_commitment=commitment,destination=tmp_path/'store',env=environment('soxx_capture_only'))
        assert result is materialize.return_value
        fetch.assert_called_once_with('secret','soxx_capture_only',C,commitment)
        materialize.assert_called_once_with('secret','soxx_capture_only',C,S,D,tmp_path/'store')


def test_invalid_identity_prevents_even_private_sidecar_read(tmp_path):
    m=module();_,commitment=private_receipt();env=environment('soxx_capture_only');env['GITHUB_RUN_ATTEMPT']='2'
    with patch.object(m,'fetch_private_receipt') as fetch, patch.object(m,'materialize_store') as materialize:
        with pytest.raises(m.PrivatePreflightError):
            m.prepare_hosted_authority(token='secret',route='soxx_capture_only',compute_sha=C,
                expected_commitment=commitment,destination=tmp_path/'store',env=env)
        fetch.assert_not_called();materialize.assert_not_called()


def test_sidecar_failure_is_quiet_and_never_materializes(tmp_path,capsys):
    m=module();_,commitment=private_receipt()
    with patch.object(m,'fetch_private_receipt',side_effect=RuntimeError('PRIVATE-PINS')) as fetch, patch.object(m,'materialize_store') as materialize:
        with pytest.raises(m.PrivatePreflightError) as caught:
            m.prepare_hosted_authority(token='secret',route='soxx_capture_only',compute_sha=C,
                expected_commitment=commitment,destination=tmp_path/'store',env=environment('soxx_capture_only'))
        assert caught.value.__context__ is None
        assert fetch.call_count == 1;materialize.assert_not_called()
    assert capsys.readouterr() == ('','')
