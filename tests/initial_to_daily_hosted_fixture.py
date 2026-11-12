"""One synthetic Store: approved initial read -> actual next-day daily owner.

All market responses and HTTP are synthetic callbacks. No external requests.
Protocol writes/reads use the actual initial and hosted daily implementations,
ordinary fresh clones and the existing exact remote adapter against a bare repo.
"""
import copy,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from scripts import provider_basis_snapshot as B
from scripts.initial_basis_remote import InitialReadGitStore
from scripts.initial_basis_delivery import current_initial_delivery
from test_initial_basis_read import fixture,runtime
from test_initial_basis_delivery import ReadAPI
from test_daily_basis_remote import TOKEN
import daily_basis_hosted_fixture as H


def run(destination):
    root=Path(destination);root.mkdir(parents=True,exist_ok=True);(root/'initial').mkdir()
    initial=fixture(root/'initial')
    # Only grant/calendar templates are synthetic. The grant binds the actual
    # initial Store selector and baseline bytes, not a fabricated daily output.
    _,grant,calendar,days,values,response=H.seed(root/'template')
    grant=copy.deepcopy(grant);calendar=copy.deepcopy(calendar)
    grant['initial_selector']=initial['grant']['selector']
    baseline_commit=H.git(initial['repo'],'rev-list','--max-parents=0','main')
    grant['baseline_ref']['commit']=baseline_commit
    grant['slots']=grant['slots'][:1]
    grant['request_limit']=grant['credit_limit']=grant['verification_limit']=1
    calendar['sessions']=calendar['sessions'][:-1]
    grant['calendar_sha256']=B.sha(B.encode(calendar))
    original_seed=H.seed
    H.seed=lambda ignored:(initial['repo'],grant,calendar,days[:-1],values[:-1],response)
    try:chain=H.HostedChain(root/'chain')
    finally:H.seed=original_seed
    checkout,transport=chain.fresh('initial-read-clone')
    transport.record['permissions']['push']=False
    reader=InitialReadGitStore(checkout.repo,TOKEN,source_recheck=lambda:None,platform_guard=lambda:None,expected_repository_id=11,expected_owner_id=12,transport=transport)
    ig=initial['grant'];approval=runtime(initial).approval
    resolved=current_initial_delivery(store=reader,read_api=ReadAPI(initial),approval=approval,
        grant_raw=B.encode(ig),calendar_raw=B.encode(initial['calendar']),consumer_sha=ig['consumer_sha'],
        trade_date=ig['trade_date'],knowledge_cutoff=initial['now'],now=initial['now'])
    pp=ig['proof_ref'];other='data/market-data/fixtures/OTHER.json'
    first=dict(binding=resolved['binding'],grant=ig,calendar=initial['calendar'],repository=initial['repository'],
        run=initial['run'],jobs=initial['jobs'],log=initial['log'],now=initial['now'],
        current_store_sha=resolved['current_store_sha'],proof=initial['proof'],proof_path=pp['path'],
        proof_blob=pp['blob_sha'],other_ref=dict(path=other,blob_sha=B.blob_sha(reader.read(resolved['current_store_sha'],other))),
        days=initial['calendar'][ig['trade_date']]['previous_sessions'],store_path=str(reader.repo),
        provider_request_count=0,read_dispatch_count=0,remote_write_count=len(transport.pushes))
    assert len(resolved['view']['records'])==60 and not transport.pushes
    # Actual durable claim/budget, one capture callback, literal retention,
    # immutable candidate/receipt, dispatch writer, private emitter and promotion.
    chain.run(0)
    daily=chain.read(later=True)
    assert daily['capture_count']==daily['dispatch_count']==1
    assert len(daily['view']['records'])==61
    assert daily['candidate']['selector']['basis_id']!=ig['selector']['basis_id']
    fresh,tr=chain.fresh('old-initial-after-takeover')
    try:
        current_initial_delivery(store=fresh,read_api=ReadAPI(initial),approval=approval,
            grant_raw=B.encode(ig),calendar_raw=B.encode(initial['calendar']),consumer_sha=ig['consumer_sha'],
            trade_date=ig['trade_date'],knowledge_cutoff=chain.now.isoformat(),now=chain.now.isoformat())
    except ValueError:rejected=True
    else:raise AssertionError('old initial date/expiry accepted after daily takeover')
    return dict(initial=first,daily=daily,initial_rejected_after_takeover=rejected,
        synthetic_provider_callbacks=len(chain.calls),synthetic_dispatch_callbacks=len(chain.posts),
        external_provider_requests=0,external_dispatches=0,clone_names=chain.clones)


if __name__=='__main__':print(json.dumps(run(sys.argv[1])))
