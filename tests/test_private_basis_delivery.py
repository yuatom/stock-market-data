import json,os,subprocess,sys
from pathlib import Path
import pytest
from test_provider_basis_snapshot import fixture_repo,git,enc
from scripts import provider_basis_snapshot as B
from scripts.emit_basis_consumer_delivery import build_delivery,encode_delivery,materialize_local_receipt_ref


def fixture(tmp_path):
    repo,sel,_,_,_=fixture_repo(tmp_path)
    receipt=enc(dict(schema_version=1,kind='basis_consumer_receipt',compute_sha='c'*40,selector=sel,admission_blob_sha=sel['admission_blob_sha'],nonce='b'*64,valid_from='2026-01-07T13:00:00Z',valid_until='2026-01-07T15:00:00Z'))
    p=repo/'basis-authority/receipt.json';p.parent.mkdir();p.write_bytes(receipt)
    git(repo,'add','.');git(repo,'commit','-qm','synthetic private receipt')
    commitment=B.receipt_commitment(receipt)
    git(repo,'update-ref',f'refs/remotes/origin/basis-read-receipts/{"c"*40}/{commitment}','HEAD')
    context=dict(repository='yuatom/stock-market-data-store',repository_id=11,owner_id=12,private=True,workflow_source_sha='d'*40,run_id=13,run_attempt=1,event='workflow_dispatch',actor_id=12,actor='yuatom',triggering_actor='yuatom',workflow_path='.github/workflows/basis-consumer-delivery.yml',job='verify-basis-consumer')
    return repo,commitment,context


def test_actions_remote_only_ref_is_materialized_and_verified(tmp_path):
    repo,commitment,context=fixture(tmp_path)
    before=git(repo,'rev-parse','HEAD')
    value=build_delivery(repo,commitment,'c'*40,context,now='2026-01-07T14:00:00Z')
    assert value['receipt_commit']==before and git(repo,'rev-parse','HEAD')==before
    assert git(repo,'status','--porcelain')==''
    assert 'nonce' not in value and 'raw_response' not in value
    assert encode_delivery(value).count('\n')==0
    assert git(repo,'rev-parse',value['receipt_ref'])==before


@pytest.mark.parametrize('field,value',[('private',False),('event','push'),('run_attempt',2),('actor_id',99),('triggering_actor','other'),('workflow_path','.github/workflows/other.yml')])
def test_private_emitter_rejects_wrong_runtime_context(tmp_path,field,value):
    repo,commitment,context=fixture(tmp_path);context[field]=value
    with pytest.raises(B.BasisError):build_delivery(repo,commitment,'c'*40,context,now='2026-01-07T14:00:00Z')


def test_conflicting_local_authority_ref_is_never_overwritten(tmp_path):
    repo,commitment,context=fixture(tmp_path)
    target=f'refs/heads/basis-read-receipts/{"c"*40}/{commitment}'
    git(repo,'update-ref',target,'HEAD^');before=git(repo,'rev-parse',target)
    with pytest.raises(B.BasisError):materialize_local_receipt_ref(repo,commitment,'c'*40)
    assert git(repo,'rev-parse',target)==before


def test_private_log_delivery_reaches_actual_consumer(tmp_path):
    consumer=os.environ.get('BASIS_CONSUMER_ROOT')
    if not consumer:pytest.skip('cross-repository consumer checkout not supplied')
    repo,commitment,context=fixture(tmp_path)
    value=build_delivery(repo,commitment,'c'*40,context,now='2026-01-07T14:00:00Z')
    payload=tmp_path/'delivery.json';payload.write_text(json.dumps({'value':value,'log':encode_delivery(value)}))
    code='''
import json,sys
from scripts.private_basis_delivery import DeliveryExpectation,verify_private_delivery
v=json.load(open(sys.argv[1]));x=v['value'];c=x['context'];sha=c['workflow_source_sha']
e=DeliveryExpectation(11,12,14,sha,'c'*40,x['receipt_commitment'],13)
repo=dict(id=11,private=True,full_name='yuatom/stock-market-data-store',owner={'id':12})
run=dict(id=13,workflow_id=14,head_sha=sha,path=c['workflow_path'],event='workflow_dispatch',run_attempt=1,status='completed',conclusion='success',head_branch='basis-consumer-delivery/'+sha,repository={'id':11},head_repository={'id':11},actor={'id':12},triggering_actor={'id':12},run_started_at='2026-01-07T13:59:00Z')
job=dict(id=15,run_id=13,name='verify-basis-consumer',head_sha=sha,status='completed',conclusion='success',completed_at='2026-01-07T14:01:00Z',steps=[dict(name='Verify and emit private delivery',status='completed',conclusion='success')])
result,authority=verify_private_delivery(e,repo,run,{'total_count':1,'jobs':[job]},{'job_id':15,'text':'2026-01-07T14:00:00.000Z '+v['log']},now='2026-01-07T14:02:00Z',calendar_authority={'2026-01-07':{'independently_reviewed_synthetic':True}})
assert result==x and authority.compute_sha=='c'*40 and authority.calendar_by_trade_date
print('PRIVATE_DELIVERY_E2E_PASS')
'''
    env=dict(os.environ,PYTHONPATH=consumer)
    result=subprocess.run([sys.executable,'-c',code,str(payload)],cwd=consumer,env=env,capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert result.stdout.strip()=='PRIVATE_DELIVERY_E2E_PASS'
