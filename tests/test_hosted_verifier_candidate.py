from tempfile import TemporaryDirectory
from pathlib import Path
from unittest import TestCase
from scripts import provider_basis_snapshot as B
from scripts.hosted_verifier_candidate import verify_hosted_pair
from scripts.daily_basis_delivery import DeliveryExpectation, WORKFLOW, MARKER
from subprocess import check_output

def git(repo, *args):
    return check_output(["git","-C",str(repo),"-c","user.name=Synthetic","-c","user.email=sim@example.invalid",*args], text=True).strip()
from base64 import b64encode

def fixture(tmp):
    repo=Path(tmp)
    git(repo,"init","-q")
    selector=dict(schema_version=1,provider=B.PROVIDER,symbol="SOXX",basis_id="7"*64,
        snapshot_blob_sha="8"*40,admission_blob_sha="9"*40,read_sha="6"*40)
    path=repo/WORKFLOW
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text('name: synthetic private hosted verifier\njobs:\n  verify-basis-consumer:\n    runs-on: ubuntu-latest\n')
    git(repo,'add','.');git(repo,'commit','-qm','synthetic source')
    source=git(repo,'rev-parse','HEAD')
    blob=git(repo,'rev-parse',f'{source}:{WORKFLOW}')
    profile=dict(kind='private_github_hosted_v1',workflow_blob=blob,expires='2026-01-07T14:10:00Z')
    repository=dict(id=11,private=True,full_name='yuatom/stock-market-data-store',owner={'id':12})
    def phase(r,j,c,commit,verify,begin,end):
        expectation=DeliveryExpectation(11,12,415302427,source,'c'*40,c,r)
        branch='basis-consumer-delivery/'+source
        run=dict(id=r,workflow_id=415302427,head_sha=source,path=WORKFLOW,event='workflow_dispatch',
          run_attempt=1,status='completed',conclusion='success',head_branch=branch,
          repository={'id':11},head_repository={'id':11},actor={'id':12},triggering_actor={'id':12},
          created_at=begin,run_started_at=begin)
        jobs=dict(total_count=1,jobs=[dict(id=j,run_id=r,name='verify-basis-consumer',head_sha=source,
          status='completed',conclusion='success',completed_at=end,labels=['ubuntu-latest'],
          steps=[dict(name='Verify and emit private delivery',status='completed',conclusion='success')])])
        context=dict(repository=repository['full_name'],repository_id=11,owner_id=12,private=True,
          workflow_source_sha=source,run_id=r,run_attempt=1,event='workflow_dispatch',actor_id=12,
          actor='yuatom',triggering_actor='yuatom',workflow_path=WORKFLOW,job='verify-basis-consumer')
        value=dict(schema_version=1,kind='private_basis_consumer_delivery',compute_sha='c'*40,receipt_commitment=c,
          context=context,receipt_ref=f'refs/heads/basis-read-receipts/{"c"*40}/{c}',receipt_commit=commit,
          receipt_blob_sha=commit,selector=selector,verified_at=verify,valid_until='2026-01-07T14:16:00Z')
        log=dict(job_id=j,text=MARKER+b64encode(B.encode(value)).decode())
        return (expectation,run,jobs,log,(commit,commit,selector))
    ready=phase(51,52,'a'*64,'1'*40,'2026-01-07T14:01:10Z','2026-01-07T14:01:00Z','2026-01-07T14:01:20Z')
    final=phase(61,62,'b'*64,'2'*40,'2026-01-07T14:03:10Z','2026-01-07T14:03:00Z','2026-01-07T14:03:20Z')
    return dict(profile=profile,repository=repository,ready=ready,final=final,
      source_readback=lambda sha,path: (git(repo,'rev-parse',f'{sha}:{path}'),git(repo,'show',f'{sha}:{path}').encode()+b'\n'),
      anchored_workflow_blob=blob,ready_dispatched_at='2026-01-07T14:00:59Z',
      provider_gate_at='2026-01-07T14:02:00Z',final_dispatched_at='2026-01-07T14:02:59Z',now='2026-01-07T14:04:00Z')

def altered(tmp_path, mutation):
    x=fixture(tmp_path)
    if mutation=='self-hosted': x['final'][2]['jobs'][0]['labels']=['self-hosted','macOS','ARM64']
    if mutation=='wrong-source': x['ready'][1]['head_sha']='4'*40
    if mutation=='attempt': x['final'][1]['run_attempt']=2
    if mutation=='wrong-purpose': x['final'][2]['jobs'][0]['steps'][0]['name']='Other'
    if mutation=='wrong-blob': x['profile']['workflow_blob']='3'*40
    if mutation=='wrong-trust': x['anchored_workflow_blob']='4'*40
    if mutation=='receipt-alias': x['final']=(*x['final'][:4],x['ready'][4])
    if mutation=='replay': x['final']=x['ready']
    if mutation=='short-runway': x['provider_gate_at']='2026-01-07T14:02:30Z'; x['profile']['expires']='2026-01-07T14:04:29Z'
    if mutation=='wrong-actor': x['final'][1]['actor']={'id':13}
    if mutation=='branch': x['ready'][1]['head_branch']='main'
    if mutation=='missing-job': x['ready'][2]['jobs']=[]
    if mutation=='wrong-job': x['final'][3]['job_id']=63
    if mutation=='expired': x['now']='2026-01-07T14:16:00Z'
    if mutation=='old-ready': x['ready_dispatched_at']='2026-01-07T14:01:02Z'
    return x

class HostedPairTest(TestCase):
    def test_pair(self):
        with TemporaryDirectory() as scratch:
            x=fixture(scratch)
            self.assertEqual(verify_hosted_pair(**x)['receipt_commit'],'2'*40)

    def test_reject(self):
        for mutation in ['self-hosted','wrong-source','attempt','wrong-purpose','wrong-blob','wrong-trust',
                'receipt-alias','replay','short-runway','wrong-actor','branch','missing-job','wrong-job','expired','old-ready']:
            with self.subTest(mutation=mutation), TemporaryDirectory() as scratch:
                x=altered(scratch,mutation)
                with self.assertRaises((B.BasisError,ValueError)):
                    verify_hosted_pair(**x)
