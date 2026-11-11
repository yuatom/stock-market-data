"""Synthetic real bare Git remotes only. Never live credentials or HTTP."""
import os
import subprocess
from unittest.mock import Mock, patch
import pytest
from tests.test_private_authority_input import fixture, COMPUTE, git, install_descriptor


def module():
    from scripts import private_authority_promotion
    return private_authority_promotion


@pytest.fixture
def remote(fixture,tmp_path,monkeypatch):
    from datetime import datetime, timezone
    monkeypatch.setattr(module(),'_utc_now',lambda:datetime(2026,11,11,13,tzinfo=timezone.utc))
    target=tmp_path/'remote.git'
    git(tmp_path,'init','--bare','-q',str(target))
    git(fixture['repo'],'push','-q',str(target),fixture['baseline']+':refs/heads/main',fixture['ref']+':'+fixture['ref'])
    return target


def exercise(f,remote,destination,*,callback=None,fault=None):
    m=module(); real_run=subprocess.run; seen=[]; pushes=[]; reads=[0]
    def network(argv,**kw):
        original=list(argv);seen.append((original,kw))
        is_network=any(x in argv for x in ('fetch','ls-remote','push'))
        if is_network:
            assert 'synthetic-token' not in ' '.join(argv)
            assert kw['env']['GIT_CONFIG_KEY_0']=='http.'+m.STORE_URL+'.extraheader'
            assert kw['capture_output'] is True
            argv=list(argv);argv[argv.index('origin')]=str(remote)
            argv[1:1]=['-c','protocol.file.allow=always']
        if 'push' in original:
            pushes.append(original)
            if fault=='race_at_push':
                real_run(['git','-C',str(remote),'update-ref','refs/heads/main',f['commit']],check=True,capture_output=True)
            if fault=='push_rejected':return subprocess.CompletedProcess(argv,1,b'',b'PRIVATE')
            if fault=='push_unknown':
                real_run(argv,**kw)
                raise subprocess.TimeoutExpired(argv,120,output=b'PRIVATE')
        if 'fetch' in original:
            reads[0]+=1
            if reads[0]>1 and fault=='readback_unknown':raise RuntimeError('PRIVATE')
            if reads[0]>1 and fault=='readback_ref_changed':
                real_run(['git','-C',str(remote),'update-ref',f['ref'],f['head']],check=True,capture_output=True)
        return real_run(argv,**kw)
    with patch.object(m,'resolve_private_reference'),patch.object(m.subprocess,'run',side_effect=network):
        try:
            result=m.materialize_and_promote(token='synthetic-token',route=f['route'],compute_sha=COMPUTE,
                authority_store_sha=f['head'],descriptor_commit=f['commit'],destination=destination,
                source_recheck=callback or (lambda:None))
            return result,seen,pushes
        finally:
            f['observed_pushes']=pushes
            f['observed_calls']=seen


def test_promotes_only_exact_single_addition_under_source_recheck(fixture,remote,tmp_path,capsys):
    order=[]
    def source():
        assert git(remote,'rev-parse','main')==fixture['baseline'];order.append('source')
    result,seen,pushes=exercise(fixture,remote,tmp_path/'checkout',callback=source)
    assert result.approval_raw==fixture['approval_raw']
    assert git(remote,'rev-parse','main')==fixture['head']
    assert git(remote,'diff-tree','--no-commit-id','--name-status','-r',fixture['head'])=='A\t'+fixture['authority_path']
    assert len(pushes)==1 and '--force-with-lease=refs/heads/main:'+fixture['baseline'] in pushes[0]
    assert fixture['head']+':refs/heads/main' in pushes[0]
    assert order==['source']
    assert len([argv for argv,_ in seen if 'fetch' in argv])==2
    assert capsys.readouterr()==('','')
    assert 'AUTHORIZATION' not in (tmp_path/'checkout/.git/config').read_text()


def test_existing_exact_authority_never_repromotes(fixture,remote,tmp_path):
    git(remote,'update-ref','refs/heads/main',fixture['head'])
    result,_,pushes=exercise(fixture,remote,tmp_path/'checkout')
    assert result.authority_store_sha==fixture['head'] and not pushes


@pytest.mark.parametrize('fault',['push_rejected','push_unknown','readback_unknown','readback_ref_changed','race_at_push'])
def test_uncertain_or_changed_remote_stops_without_retry(fixture,remote,tmp_path,fault,capsys):
    with pytest.raises(module().PrivatePromotionError) as caught:
        exercise(fixture,remote,tmp_path/'checkout',fault=fault)
    assert str(caught.value)=='PRIVATE_AUTHORITY_PROMOTION_STOPPED_INSPECT_BEFORE_RETRY'
    assert caught.value.__context__ is None
    assert len(fixture['observed_pushes'])==1
    assert capsys.readouterr()==('','')
    if fault in ('push_unknown','readback_unknown','readback_ref_changed'):
        assert git(remote,'rev-parse','main')==fixture['head']  # Never roll back unknown success.


@pytest.mark.parametrize('mutation',['stale_main','moved_descriptor','source_failure','source_changes_main','source_changes_ref'])
def test_invalid_prewrite_state_never_promotes(fixture,remote,tmp_path,mutation):
    def source():
        if mutation=='source_failure':raise ValueError('PRIVATE')
        if mutation=='source_changes_main':git(remote,'update-ref','refs/heads/main',fixture['commit'])
        if mutation=='source_changes_ref':git(remote,'update-ref',fixture['ref'],fixture['head'])
    if mutation=='stale_main':git(remote,'update-ref','refs/heads/main',fixture['commit'])
    if mutation=='moved_descriptor':git(remote,'update-ref',fixture['ref'],fixture['head'])
    with pytest.raises(module().PrivatePromotionError):exercise(fixture,remote,tmp_path/'checkout',callback=source)
    assert not fixture['observed_pushes']


def test_bad_descriptor_is_rejected_before_write_even_when_receipt_pins_it(fixture,remote,tmp_path):
    install_descriptor(fixture,extras={'unexpected':b'private'})
    git(fixture['repo'],'push','-q','--force',str(remote),fixture['ref']+':'+fixture['ref'])
    with pytest.raises(module().PrivatePromotionError):exercise(fixture,remote,tmp_path/'checkout')
    assert not fixture['observed_pushes']
    assert git(remote,'rev-parse','main')==fixture['baseline']


def test_occupied_checkout_is_never_reused(fixture,remote,tmp_path):
    target=tmp_path/'occupied';target.mkdir();(target/'keep').write_text('keep')
    with pytest.raises(module().PrivatePromotionError):exercise(fixture,remote,target)
    assert not fixture['observed_pushes'] and (target/'keep').read_text()=='keep'


def test_hosted_identity_rejected_before_receipt_or_promotion(tmp_path):
    m=module()
    with patch.object(m,'fetch_private_receipt') as fetch,patch.object(m,'materialize_and_promote') as promote:
        with pytest.raises(m.PrivatePromotionError):
            m.prepare_promoted_authority(token='synthetic-token',route='soxx_capture_only',compute_sha=COMPUTE,
                expected_commitment='a'*64,destination=tmp_path/'private',source_recheck=lambda:None,env={})
        fetch.assert_not_called();promote.assert_not_called()


@pytest.mark.parametrize('instant',['2026-11-11T11:59:59+00:00','2026-11-11T23:00:00+00:00'])
def test_outside_approved_window_never_promotes(fixture,remote,tmp_path,monkeypatch,instant):
    from datetime import datetime
    monkeypatch.setattr(module(),'_utc_now',lambda:datetime.fromisoformat(instant))
    with pytest.raises(module().PrivatePromotionError):exercise(fixture,remote,tmp_path/'checkout')
    assert not fixture['observed_pushes']
    assert git(remote,'rev-parse','main')==fixture['baseline']


def test_hosted_receipt_binding_before_promotion(tmp_path):
    from scripts.private_authority_receipt import canonical_receipt,receipt_commitment
    from tests.test_private_authority_preflight import environment
    m=module();callback=Mock()
    raw=canonical_receipt(dict(schema_version=1,kind='capture_authority_receipt',route='soxx_capture_only',
        compute_sha=COMPUTE,authority_store_sha='a'*40,descriptor_commit='d'*40,nonce='1'*64))
    with patch.object(m,'fetch_private_receipt',return_value=raw),patch.object(m,'materialize_and_promote') as promote:
        result=m.prepare_promoted_authority(token='synthetic-token',route='soxx_capture_only',compute_sha=COMPUTE,
            expected_commitment=receipt_commitment(raw),destination=tmp_path/'private',source_recheck=callback,
            env=environment('soxx_capture_only'))
        assert result is promote.return_value
        assert promote.call_args.kwargs['source_recheck'] is callback
        assert promote.call_args.kwargs['authority_store_sha']=='a'*40
        assert promote.call_args.kwargs['descriptor_commit']=='d'*40
    with patch.object(m,'fetch_private_receipt',return_value=raw),patch.object(m,'materialize_and_promote') as promote:
        with pytest.raises(m.PrivatePromotionError):
            m.prepare_promoted_authority(token='synthetic-token',route='soxx_capture_only',compute_sha=COMPUTE,
                expected_commitment='0'*64,destination=tmp_path/'private',source_recheck=callback,
                env=environment('soxx_capture_only'))
        promote.assert_not_called()


def test_expiry_during_source_recheck_stops_before_push(fixture,remote,tmp_path,monkeypatch):
    from datetime import datetime, timezone
    def source():
        monkeypatch.setattr(module(),'_utc_now',lambda:datetime(2026,11,11,23,tzinfo=timezone.utc))
    with pytest.raises(module().PrivatePromotionError):
        exercise(fixture,remote,tmp_path/'checkout',callback=source)
    assert not fixture['observed_pushes']
    assert git(remote,'rev-parse','main')==fixture['baseline']


def test_freshness_binds_et_execution_day_not_utc_date(monkeypatch):
    from datetime import datetime, timezone
    from types import SimpleNamespace
    import json
    m=module()
    selection=SimpleNamespace(approval_raw=json.dumps(dict(valid_from='2026-11-10T00:00:00+00:00',
        valid_until='2026-11-12T00:00:00+00:00',execution_day='2026-11-11')).encode())
    monkeypatch.setattr(m,'_utc_now',lambda:datetime(2026,11,11,1,tzinfo=timezone.utc))
    with pytest.raises(ValueError):m._fresh(selection)  # Still November 10 in New York.
    monkeypatch.setattr(m,'_utc_now',lambda:datetime(2026,11,11,13,tzinfo=timezone.utc))
    m._fresh(selection)


def test_network_auth_is_ephemeral_and_local_selector_is_unauthenticated(fixture,remote,tmp_path,monkeypatch):
    monkeypatch.setenv('GIT_TRACE','/tmp/not-created-by-promotion')
    monkeypatch.setenv('HTTPS_PROXY','https://example.invalid')
    _,seen,_=exercise(fixture,remote,tmp_path/'checkout')
    transport=[(argv,kw) for argv,kw in seen if 'http.sslVerify=true' in argv]
    for argv,kw in transport:
        assert not any(k.lower().endswith('_proxy') for k in kw['env'])
        assert 'GIT_TRACE' not in kw['env']
        assert ('GIT_CONFIG_VALUE_0' in kw['env']) == any(x in argv for x in ('fetch','ls-remote','push'))
    assert (tmp_path/'checkout').stat().st_mode & 0o777 == 0o700
