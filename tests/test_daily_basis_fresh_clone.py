from datetime import datetime, timezone
from scripts import daily_basis_updates as D
from scripts.daily_basis_remote import RemoteGitStore,STORE_URL
from scripts import provider_basis_snapshot as B
from test_daily_basis_updates import setup,capture,synthetic_delivery,git
from test_daily_basis_remote import BareTransport,TOKEN

def test_second_daily_slot_from_fresh_hosted_clone(tmp_path):
    _,repo,g,c,paths,store,now,u=setup(tmp_path)
    with store.lock():
        first=u.run_slot('nov10','101-1',capture=capture())
        proof=synthetic_delivery(D,u,first,now[0]);u.promote(first,proof)
    remote=tmp_path/'remote.git'
    git(tmp_path,'clone','--bare','--no-hardlinks',str(repo),str(remote))
    fresh=tmp_path/'fresh-clone'
    git(tmp_path,'clone','--no-hardlinks',str(remote),str(fresh))
    git(fresh,'remote','set-url','origin',STORE_URL)
    transport=BareTransport(fresh,remote)
    remote_store=RemoteGitStore(fresh,TOKEN,source_recheck=lambda:None,platform_guard=lambda:None,expected_repository_id=11,expected_owner_id=12,transport=transport)
    now[0]=datetime(2026,11,11,12,0,5,tzinfo=timezone.utc)
    current=D.DailyUpdater(remote_store,B.encode(g),B.encode(c),u.authority,clock=lambda:now[0])
    prior_ref=u._candidate_ref(u._slot('nov10'))
    assert remote_store.remote_ref(prior_ref)==first.selector['read_sha']
    assert remote_store.ref(prior_ref) is None
    assert git(fresh,'rev-parse',prior_ref.replace('refs/heads/','refs/remotes/origin/'))==first.selector['read_sha']
    calls=[]
    with remote_store.lock():
        second=current.run_slot('nov11','102-1',capture=capture(calls=calls))
    assert len(calls)==1 and second.selector['basis_id']!=first.selector['basis_id']


def test_actual_hosted_route_two_fresh_days_and_separate_current_reader(tmp_path):
    from daily_basis_hosted_fixture import HostedChain
    chain=HostedChain(tmp_path)
    chain.run(0);chain.run(1);result=chain.read()
    assert result['capture_count']==result['dispatch_count']==2
    assert result['clone_names']==['collector-sep10','verifier-555','collector-sep11','verifier-565','other-symbol-writer','standalone-reader']
    assert len(result['view']['records'])==62
    assert result['view']['records'][-1]['trade_date']=='2026-09-10'
    assert result['current_store_sha']!=result['candidate']['selector']['read_sha']
    assert set(result['mapping'])=={'schema_version','kind','candidate','run_id','dispatched_at'}


import pytest


@pytest.mark.parametrize('attack',['missing','changed_during_fetch','conflicting_local'])
def test_fresh_second_slot_ref_uncertainty_stops_before_debit_or_provider(tmp_path,attack):
    from daily_basis_hosted_fixture import HostedChain
    from scripts.run_daily_basis_route import HostedDailyError
    chain=HostedChain(tmp_path);chain.run(0)
    digest=B.sha(B.encode(chain.grant));ref='refs/heads/basis-daily-candidates/'+digest+'/sep10'
    old=chain.grant['initial_selector']['read_sha']
    head=git(chain.remote,'rev-parse','main')
    if attack=='missing':git(chain.remote,'update-ref','-d',ref)
    def before(store,tr):
        if attack=='conflicting_local':git(store.repo,'update-ref',ref,old)
        if attack=='changed_during_fetch':
            original=tr.git
            def wrapped(*args):
                if args[0]=='fetch' and args[-1]==ref:
                    tr.after_fetch=lambda:git(chain.remote,'update-ref',ref,old)
                return original(*args)
            tr.git=wrapped
    with pytest.raises(HostedDailyError):chain.run(1,before=before)
    assert len(chain.calls)==len(chain.posts)==1
    assert git(chain.remote,'rev-parse','main')==head
    assert git(chain.remote,'ls-tree','-r','--name-only','main','--','data/market-data/collector-state/2026-09/2026-09-11-twelve-data-budget.json')==''
    assert not (tmp_path/'delegation-sep11.json').exists()


def test_standalone_reader_materializes_all_remote_protocol_refs_without_dispatch(tmp_path):
    from daily_basis_hosted_fixture import HostedChain
    chain=HostedChain(tmp_path);chain.run(0);chain.run(1);result=chain.read()
    repo=result['store_path'];g=result['grant'];digest=result['candidate']['grant_sha256']
    required=[f'refs/heads/basis-daily-candidates/{digest}/sep10',f'refs/heads/basis-daily-candidates/{digest}/sep11',f'refs/heads/basis-daily-attempts/{digest}/sep11',f'refs/heads/basis-daily-verification/{digest}/sep11',f'refs/heads/basis-daily-deliveries/{digest}/sep11',result['candidate']['receipt_ref']]
    for ref in required:
        assert git(repo,'rev-parse',ref)==git(chain.remote,'rev-parse',ref)
    assert result['capture_count']==result['dispatch_count']==2
    assert git(repo,'rev-parse','refs/tags/market-data-read/'+result['current_store_sha'])==result['current_store_sha']
