import copy
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.raw_response_evidence import build_envelope
from scripts import provider_basis_snapshot as basis


def enc(v):
    return (json.dumps(v, sort_keys=True, separators=(',', ':'), ensure_ascii=True)+'\n').encode()


def evidence(symbol='AAA'):
    request=dict(provider='twelve_data',symbol=symbol,interval='1day',exchange='NASDAQ',
                 mic_code='XNAS',currency='USD',type='ETF',adjust='splits',dp=-1,
                 start_date='2026-01-01',end_date='2026-01-06',outputsize=5000,order='asc')
    rows=[dict(datetime=d,open=p,high=p,low=p,close=p,volume=v) for d,p,v in (
        ('2026-01-01','10.00000','300'),('2026-01-02','11.000001','303'),
        ('2026-01-05','12.00','120'),('2026-01-06','13','130'))]
    raw=enc(dict(status='ok',meta={k:request[k] for k in ('symbol','interval','currency','exchange','mic_code','type')}|
                 {'exchange_timezone':'America/New_York'},values=rows))
    envelope=build_envelope(raw,request,'2026-01-07T12:00:00Z')
    old=enc([dict(trade_date=d,open=p,high=p,low=p,close=p,volume=100) for d,p in (
        ('2026-01-01',30),('2026-01-02',33))])
    event=dict(kind='forward_split',numerator=3,denominator=1,effective_date='2026-01-05',
               source_url='https://example.invalid/issuer/split')
    return envelope,old,event


def candidate():
    e,b,event=evidence()
    return basis.prepare_snapshot(e,hashlib.sha256(e).hexdigest(),b,event)


def git(repo,*args):
    return subprocess.check_output(['git','-C',str(repo),'-c','user.name=Test','-c','user.email=test@example.invalid',*args]).decode().strip()


def fixture_repo(tmp_path, *, source_symbol='AAA', mutation=None, early_utc=False):
    e,old,event=evidence(source_symbol)
    if early_utc:
        doc=json.loads(e);payload=json.loads(__import__('base64').b64decode(doc['raw_response_base64']))
        payload['values'][-1]['datetime']='2026-01-07';doc['request']['end_date']='2026-01-07'
        e=build_envelope(enc(payload),doc['request'],'2026-01-07T00:01:00Z')
    snapshot=basis.prepare_snapshot(e,hashlib.sha256(e).hexdigest(),old,event)
    snapshot['symbol']='AAA'
    if mutation:mutation(snapshot)
    snapshot['basis_id']=hashlib.sha256(enc({k:v for k,v in snapshot.items() if k!='basis_id'})).hexdigest()
    bid=snapshot['basis_id']
    repo=tmp_path/'store';repo.mkdir();git(repo,'init','-q');git(repo,'remote','add','origin','https://github.com/yuatom/stock-market-data-store')
    envelope_path='capture-evidence/synthetic/envelope.json'
    terminal_path='capture-evidence/synthetic/terminal.json'
    terminal=enc(dict(status='CAPTURE_RETAINED_UNQUALIFIED',request_id='123-1',compute_sha='a'*40,
        raw_response_sha256=snapshot['raw_response_sha256'],envelope_sha256=snapshot['envelope_sha256']))
    admission=dict(schema_version=1,kind='provider_snapshot_price_admission',status='ADMITTED_PRICE_ONLY',
        basis_id=bid,allowed_fields=['open','high','low','close'],volume_policy='BLOCKED_UNRESOLVED',
        admitted_at=('2026-01-07T00:02:00Z' if early_utc else '2026-01-07T13:00:00Z'),proof_model='reviewed_workflow_receipt_not_provider_signature',
        envelope_ref={'path':envelope_path,'blob_sha':basis.blob_sha(e)},
        workflow_evidence={'compute_sha':'a'*40,'request_id':'123-1','terminal_ref':{'path':terminal_path,'blob_sha':basis.blob_sha(terminal)}},
        unresolved_volume_dates=snapshot['unresolved_volume_dates'])
    sr=enc(snapshot);ar=enc(admission)
    catalog=dict(schema_version=1,provider='twelve_data_basic',symbol='AAA',basis_id=bid,
                 snapshot_blob_sha=basis.blob_sha(sr),admission_blob_sha=basis.blob_sha(ar))
    paths={basis.snapshot_path('twelve_data_basic','AAA',bid):sr,
           basis.admission_path('twelve_data_basic','AAA',basis.blob_sha(ar)):ar,
           basis.catalog_path('twelve_data_basic','AAA'):enc(catalog),envelope_path:e,terminal_path:terminal}
    for p,raw in paths.items():
        f=repo/p;f.parent.mkdir(parents=True,exist_ok=True);f.write_bytes(raw)
    git(repo,'add','.');git(repo,'commit','-qm','synthetic')
    selection=catalog|{'read_sha':git(repo,'rev-parse','HEAD')}
    authority=basis.ReadAuthority(hashlib.sha256(enc(selection)).hexdigest(),selection['admission_blob_sha'])
    return repo,selection,authority,snapshot,paths


def read(repo,selection,authority,**kwargs):
    return basis.read_price_view(repo,selection,authority,fields=kwargs.pop('fields',('close',)),
       required_sessions=kwargs.pop('required_sessions',('2026-01-01','2026-01-02','2026-01-05','2026-01-06')),
       knowledge_cutoff=kwargs.pop('knowledge_cutoff','2026-01-07T14:00:00Z'),**kwargs)


def test_preserves_literal_provider_values_and_flags_unexplained_volume():
    s=candidate();assert s['records']==json.loads(evidence()[0])['records']
    assert s['unresolved_volume_dates']==['2026-01-02']
    assert s['volume_differences'][0]['difference_from_expected']=='3'
    assert s['price_equivalence']=='NOT_ASSERTED'
    assert s['market_qualified'] is False
    assert s['records'][1]['close']=='11.000001'


def test_staging_is_exclusive_and_verifies_content(tmp_path):
    s=candidate();out=tmp_path/'candidate';basis.stage_snapshot(s,out)
    assert basis.verify_staged_snapshot(out,s['basis_id'])==s
    with pytest.raises(basis.BasisError):basis.stage_snapshot(s,out)
    f=out/'snapshot.json';f.write_bytes(f.read_bytes().replace(b'11.000001',b'11.000002'))
    with pytest.raises(basis.BasisError):basis.verify_staged_snapshot(out,s['basis_id'])


def test_bad_ohlc_rejected_before_staging():
    e,b,event=evidence();doc=json.loads(e);raw=json.loads(__import__('base64').b64decode(doc['raw_response_base64']))
    raw['values'][0]['high']='1'
    malformed=build_envelope(enc(raw),doc['request'],doc['observed_at'])
    with pytest.raises(basis.BasisError):basis.prepare_snapshot(malformed,hashlib.sha256(malformed).hexdigest(),b,event)


def test_price_projection_requires_exact_git_and_omits_other_fields(tmp_path):
    repo,sel,auth,s,_=fixture_repo(tmp_path)
    result=read(repo,sel,auth,fields=('open','close'))
    assert len(result['records'])==4
    assert set(result['records'][0])=={'trade_date','open','close'}
    assert result['records'][1]['close']=='11.000001'
    assert result['last_trade_date']=='2026-01-06'
    before=read(repo,sel,auth)
    (repo/basis.snapshot_path('twelve_data_basic','AAA',s['basis_id'])).write_text('worktree tamper')
    assert read(repo,sel,auth)==before


@pytest.mark.parametrize('fields',[('volume',),('close','volume'),('dollar_volume',),(),('close','close')])
def test_volume_or_invalid_projection_fails_closed(tmp_path,fields):
    repo,s,a,_,_=fixture_repo(tmp_path)
    with pytest.raises(basis.BasisError):read(repo,s,a,fields=fields)


def test_missing_date_and_lookahead_fail_closed(tmp_path):
    repo,s,a,_,_=fixture_repo(tmp_path)
    with pytest.raises(basis.BasisError):read(repo,s,a,required_sessions=('2026-01-06','2026-01-07'))
    with pytest.raises(basis.BasisError):read(repo,s,a,knowledge_cutoff='2026-01-06T23:00:00Z')
    with pytest.raises(basis.BasisError):read(repo,s,a,required_sessions=('2026-01-06','2026-01-05'))


def test_self_supplied_or_wrong_selection_not_authority(tmp_path):
    repo,s,a,_,_=fixture_repo(tmp_path)
    with pytest.raises(basis.BasisError):read(repo,s,None)
    wrong=s|{'basis_id':'b'*64}
    with pytest.raises(basis.BasisError):read(repo,wrong,a)
    with pytest.raises(basis.BasisError):read(repo,s,basis.ReadAuthority('0'*64,s['admission_blob_sha']))


def test_catalog_and_admission_must_be_bound(tmp_path):
    repo,s,a,_,_=fixture_repo(tmp_path)
    p=repo/basis.catalog_path('twelve_data_basic','AAA');p.write_text('{}')
    git(repo,'add','.');git(repo,'commit','-qm','bad catalog')
    bad=s|{'read_sha':git(repo,'rev-parse','HEAD')};auth=basis.ReadAuthority(hashlib.sha256(enc(bad)).hexdigest(),s['admission_blob_sha'])
    with pytest.raises(basis.BasisError):read(repo,bad,auth)


def test_private_receipt_commits_to_source_selection_and_freshness(tmp_path):
    repo,s,a,_,_=fixture_repo(tmp_path)
    r=dict(schema_version=1,kind='basis_consumer_receipt',compute_sha='a'*40,selector=s,
        admission_blob_sha=s['admission_blob_sha'],nonce='b'*64,
        valid_from='2026-01-07T13:00:00Z',valid_until='2026-01-07T15:00:00Z')
    raw=enc(r);commitment=basis.receipt_commitment(raw)
    selection,authority=basis.verify_consumer_receipt(raw,commitment,'a'*40,now='2026-01-07T14:00:00Z')
    assert selection==s and read(repo,s,authority)['record_count']==4
    for digest,source,now in [('0'*64,'a'*40,'2026-01-07T14:00:00Z'),(commitment,'c'*40,'2026-01-07T14:00:00Z'),(commitment,'a'*40,'2026-01-07T15:00:00Z')]:
        with pytest.raises(basis.BasisError):basis.verify_consumer_receipt(raw,digest,source,now=now)
    assert s['basis_id'] not in repr(authority)


def test_private_receipt_local_ref_adapter_no_network(tmp_path):
    repo,s,a,_,_=fixture_repo(tmp_path)
    raw=enc(dict(schema_version=1,kind='basis_consumer_receipt',compute_sha='a'*40,selector=s,
        admission_blob_sha=s['admission_blob_sha'],nonce='b'*64,
        valid_from='2026-01-07T13:00:00Z',valid_until='2026-01-07T15:00:00Z'))
    digest=basis.receipt_commitment(raw);p=repo/'basis-authority/receipt.json';p.parent.mkdir();p.write_bytes(raw)
    git(repo,'add','.');git(repo,'commit','-qm','private synthetic receipt')
    ref=f'refs/heads/basis-read-receipts/{"a"*40}/{digest}';git(repo,'update-ref',ref,'HEAD')
    selected,authority=basis.load_consumer_receipt(repo,digest,'a'*40,now='2026-01-07T14:00:00Z')
    assert selected==s and read(repo,s,authority)['record_count']==4


def test_valid_other_instrument_capture_cannot_be_relabelled(tmp_path):
    repo,selector,authority,_,_=fixture_repo(tmp_path,source_symbol='BBB')
    with pytest.raises(basis.BasisError):read(repo,selector,authority)


@pytest.mark.parametrize('mutation',[
    lambda s:s.update(basis_semantics='raw_unadjusted'),
    lambda s:s.update(unknown_contract_field=True),
])
def test_rehashed_snapshot_invalid_schema_or_semantics_rejected(tmp_path,mutation):
    repo,selector,authority,_,_=fixture_repo(tmp_path,mutation=mutation)
    with pytest.raises(basis.BasisError):read(repo,selector,authority)


def test_us_session_date_uses_new_york_not_utc(tmp_path):
    repo,s,a,_,_=fixture_repo(tmp_path,early_utc=True)
    with pytest.raises(basis.BasisError):
        read(repo,s,a,knowledge_cutoff='2026-01-07T00:03:00Z',
             required_sessions=('2026-01-01','2026-01-02','2026-01-05','2026-01-07'))


def test_nonintegral_split_diagnostic_keeps_exact_rational_residual(tmp_path):
    e,b,event=evidence();event.update(numerator=4,denominator=3)
    snapshot=basis.prepare_snapshot(e,hashlib.sha256(e).hexdigest(),b,event)
    assert snapshot['volume_differences'][0]['difference_from_expected']=='500/3'
    basis.stage_snapshot(snapshot,tmp_path/'rational')
