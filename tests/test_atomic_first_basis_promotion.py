"""Synthetic no-main-catalog bootstrap. No real provider or remote writes."""
import copy
from datetime import datetime, timezone
import pytest
from test_daily_basis_updates import fixture, git, capture, synthetic_delivery
from scripts import provider_basis_snapshot as B
from scripts import daily_basis_updates as D


def unselected_setup(tmp_path):
    repo,g,c,paths=fixture(tmp_path);cat=B.catalog_path(B.PROVIDER,'SOXX')
    git(repo,'rm',cat);git(repo,'commit','-qm','unselected baseline')
    base=git(repo,'rev-parse','HEAD')
    f=repo/cat;f.parent.mkdir(parents=True,exist_ok=True);f.write_bytes(paths[cat])
    git(repo,'add',cat);git(repo,'commit','-qm','isolated approved initial candidate')
    initial=git(repo,'rev-parse','HEAD');git(repo,'branch','initial-candidate',initial)
    g['initial_selector']['read_sha']=initial
    git(repo,'reset','--hard',base)
    auth=D.GrantAuthority(B.sha(B.encode(g)),'a'*40,'b'*40,'c'*40,B.sha(B.encode(c)))
    store=D.LocalGitStore(repo);now=[datetime(2026,11,10,12,0,5,tzinfo=timezone.utc)]
    u=D.DailyUpdater(store,B.encode(g),B.encode(c),auth,clock=lambda:now[0])
    return repo,g,c,paths,store,now,u,base,initial


def test_initial_catalog_is_added_only_after_capture_and_verification(tmp_path):
    repo,g,c,paths,store,now,u,base,initial=unselected_setup(tmp_path)
    cat=B.catalog_path(B.PROVIDER,'SOXX');calls=[]
    with store.lock():
        candidate=u.run_slot('nov10','101-1',capture=capture(calls=calls))
        assert len(calls)==1 and not store.exists(store.head(),cat)
        u.validate_candidate(candidate)
        delivery=synthetic_delivery(D,u,candidate,now[0])
        assert not store.exists(store.head(),cat)
        u.promote(candidate,delivery)
        assert store.read(store.head(),cat)==B.encode({k:v for k,v in candidate.selector.items() if k!='read_sha'})
        assert len(u.read_current(delivery,fields=('close',))['records'])==5
        for p,raw in paths.items():
            if p!=cat:assert store.read(store.head(),p)==raw


def test_failed_first_capture_leaves_catalog_absent(tmp_path):
    repo,g,c,paths,store,now,u,base,initial=unselected_setup(tmp_path)
    calls=[]
    with store.lock():
        with pytest.raises(D.DailyBasisError):u.run_slot('nov10','101-1',capture=capture(fail=True,calls=calls))
        assert len(calls)==1
        assert not store.exists(store.head(),B.catalog_path(B.PROVIDER,'SOXX'))
        with pytest.raises(D.DailyBasisError):u.run_slot('nov10','102-1',capture=capture(calls=calls))
        assert len(calls)==1


def test_catalog_added_then_removed_is_not_initial_bootstrap(tmp_path):
    repo,g,c,paths,store,now,u,base,initial=unselected_setup(tmp_path)
    cat=B.catalog_path(B.PROVIDER,'SOXX');p=repo/cat;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(paths[cat])
    git(repo,'add',cat);git(repo,'commit','-qm','previous selection')
    git(repo,'rm',cat);git(repo,'commit','-qm','selection removal')
    calls=[]
    with store.lock():
        with pytest.raises(Exception):u.run_slot('nov10','101-1',capture=capture(calls=calls))
    assert not calls and not store.exists(store.head(),cat)


def test_unrelated_changes_do_not_block_first_promotion(tmp_path):
    repo,g,c,paths,store,now,u,base,initial=unselected_setup(tmp_path)
    with store.lock():
        store.publish(store.head(),{'unrelated.txt':b'preserve'},replace=set())
        candidate=u.run_slot('nov10','101-1',capture=capture())
        u.validate_candidate(candidate)
        u.promote(candidate,synthetic_delivery(D,u,candidate,now[0]))
        assert store.read(store.head(),'unrelated.txt')==b'preserve'


def test_invalid_delivery_never_selects_initial_catalog(tmp_path):
    repo,g,c,paths,store,now,u,base,initial=unselected_setup(tmp_path)
    with store.lock():
        candidate=u.run_slot('nov10','101-1',capture=capture())
        with pytest.raises(Exception):u.promote(candidate,None)
        assert not store.exists(store.head(),B.catalog_path(B.PROVIDER,'SOXX'))


def test_hosted_first_promotion_uses_private_verifier_before_selection(tmp_path,monkeypatch):
    import daily_basis_hosted_fixture as H
    original=H.seed;cat=B.catalog_path(B.PROVIDER,'SOXX')
    def seed(root):
        repo,g,c,days,values,response=original(root)
        raw=(repo/cat).read_bytes()
        git(repo,'rm',cat);git(repo,'commit','-qm','unselected baseline')
        base=git(repo,'rev-parse','HEAD')
        (repo/cat).parent.mkdir(parents=True,exist_ok=True);(repo/cat).write_bytes(raw);git(repo,'add',cat);git(repo,'commit','-qm','approved isolated candidate')
        initial=git(repo,'rev-parse','HEAD');git(repo,'branch','initial-candidate',initial)
        g['initial_selector']['read_sha']=initial;g['slots']=g['slots'][:1]
        g['request_limit']=g['credit_limit']=g['verification_limit']=1
        c['sessions']=c['sessions'][:-1];g['calendar_sha256']=B.sha(B.encode(c))
        git(repo,'reset','--hard',base)
        return repo,g,c,days,values,response
    monkeypatch.setattr(H,'seed',seed)
    chain=H.HostedChain(tmp_path/'chain');request=chain.request
    def checked_request(method,path,payload=None):
        if method=='POST':
            result=H.git(chain.remote,'ls-tree','main','--',cat)
            assert not result, 'main selected before private verification'
        return request(method,path,payload)
    chain.request=checked_request
    try:chain.run(0)
    except Exception as exc:
        while exc.__context__ is not None:exc=exc.__context__
        raise exc
    result=chain.read()
    assert result['capture_count']==result['dispatch_count']==1
    assert len(result['view']['records'])==61
    assert H.git(chain.remote,'ls-tree','main','--',cat)
