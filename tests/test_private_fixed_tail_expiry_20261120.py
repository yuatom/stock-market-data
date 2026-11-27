from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import json,os,subprocess,sys,time
from urllib.parse import urlsplit,parse_qs
from unittest.mock import patch
from contextlib import nullcontext
import tempfile
import unittest
import yaml
def fixture(case, root):
    """Use a disposable file-only Store and stop at the fake HTTP boundary."""
    orig = Path(__file__).resolve().parents[1]
    sys.path[:0]=[str(orig),str(orig/'scripts')]
    import maintain_fixed_symbols as n
    import backfill_market_data_tail as tail
    import capture_store_retention as retention
    from scripts import run_private_fixed_tail_20261120 as hosted
    from scripts import private_fixed_tail_20261120 as profile
    from scripts.market_data_store import git_blob_sha_bytes
    os.environ.update(GIT_ALLOW_PROTOCOL='file',GIT_CONFIG_GLOBAL='/dev/null',GIT_CONFIG_NOSYSTEM='1',GIT_TERMINAL_PROMPT='0',
     GIT_AUTHOR_NAME='Synthetic',GIT_COMMITTER_NAME='Synthetic',GIT_AUTHOR_EMAIL='s@example.invalid',GIT_COMMITTER_EMAIL='s@example.invalid')
    def deny(event,args):
     if event.startswith('socket.'): raise AssertionError('offline fixture: network prohibited')
    sys.addaudithook(deny)
    def git(repo,*args):
     return subprocess.run(['git','-C',str(repo),*args],text=True,capture_output=True,check=True).stdout.strip()
    def init(nm):
     repo=root/nm; origin=root/(nm+'-origin')
     repo.mkdir();origin.mkdir()
     git(repo,'init','-q','--initial-branch=main');git(origin,'init','-q','--bare','--initial-branch=main');git(repo,'remote','add','origin',str(origin))
     return repo,origin
    compute,_=init('compute-synthetic-authority')
    (compute/'config').mkdir()
    for part in ('market-data-store.yaml','market-data-collector-access.yaml','collection-universe.json'):
     (compute/'config'/part).write_bytes((orig/'config'/part).read_bytes())
    git(compute,'add','config')
    git(compute,'commit','-qm','synthetic accepted source pin for native guards')
    git(compute,'push','-q','origin','HEAD:refs/heads/main'); compute_sha=git(compute,'rev-parse','HEAD')
    repo,origin=init('store-synthetic')
    DAY='2026-11-23'; END='2026-11-20'; BASE='2026-11-19'; RUN='42-1'
    P=n.r.PREFIX
    cfg=yaml.safe_load((orig/'config/market-data-store.yaml').read_bytes())
    access=yaml.safe_load((orig/'config/market-data-collector-access.yaml').read_bytes())
    h=n.a.load_helpers()
    u=h.collection.load_collection_universe(orig/'config/collection-universe.json')
    assets=dict(h.collection.daily_universe(u))
    dates=sorted(d.isoformat() for k in range(365) if (d:=date.fromisoformat(BASE)-timedelta(days=k)).weekday()<5)[-256:]
    targets=[]
    for i,symbol in enumerate(profile.SCOPE):
     effective=h.collection.ticker_effective_at(u,symbol)
     def price(d,index):return dict(trade_date=d,open=80.+i+index,high=81.+i+index,low=79.+i+index,close=80.+i+index,volume=15)
     rows=[price(d,j) for j,d in enumerate(dates) if effective is None or d>=effective]
     h.store.append_daily_bars(repo/P,provider=profile.PROVIDER,symbol=symbol,asset_class=assets[symbol],identity_effective_from=effective,series_semantics='daily_regular_ohlcv',bars=rows)
     idx=f'{P}series/daily/{profile.PROVIDER}/{symbol}/_index.json'
     targets.append(dict(symbol=symbol,index_blob_sha=git(repo,'hash-object',idx)))
    ledger_rel=f'{P}collector-state/{DAY[:7]}/{DAY}-twelve-data-budget.json'
    prior_credits=2 if case=='wall_before_debit_publish' else 0
    prior_minutes={DAY+'T12:55Z':prior_credits} if prior_credits else {}
    if prior_credits:
     ledger=repo/ledger_rel
     ledger.parent.mkdir(parents=True,exist_ok=True)
     ledger.write_text(json.dumps(dict(schema_version=1,trade_date=DAY,credits=prior_credits,minutes=prior_minutes)))
    git(repo,'add','data');git(repo,'commit','-qm','synthetic complete previous slices')
    old=git(repo,'rev-parse','HEAD');git(repo,'push','-q','origin','HEAD:refs/heads/main')
    claim_data=dict(schema_version=1,kind='fixed_daily_tail_batch_claim',route='fixed_daily_tail_20261120',
     approval_commitment='a'*64,reviewed_store_sha=old,compute_sha=compute_sha,execution_date=DAY,historical_cutoff=END,
     request_id=RUN,physical_attempt='b'*64)
    raw=(json.dumps(claim_data,sort_keys=True,separators=(',',':'))+'\n').encode('ascii')
    receipt=retention.retain_capture_object(repo=repo,expected_commit=old,approval_sha256='a'*64,
     symbol='FIXED-TAIL-1120',name='claim.json',payload=raw,execution_day=DAY,request_id=RUN,
     forbidden_bytes=(b'fake-fixture-token',))
    op=receipt['store_commit_sha']
    assert git(repo,'rev-list','--parents','-n1',op)==f'{op} {old}'
    assert git(repo,'diff-tree','--no-commit-id','--name-status','-r',op)==f'A\t{receipt["path"]}'
    assert git(repo,'rev-parse',op+':data/market-data')==git(repo,'rev-parse',old+':data/market-data')
    print('real private Store write primitive: synthetic claim durable and only claim path',flush=True)
    now=datetime.fromisoformat(DAY+'T13:00:01+00:00')
    until=now if case=='late_handoff' else now+timedelta(seconds=5)
    if case=='late_handoff':
     now=until-timedelta(seconds=1800)
    monotonic=100.0
    calls=[]
    class FrozenDatetime(datetime):
     @classmethod
     def now(cls,tz=None):return now.astimezone(tz) if tz is not None else now.replace(tzinfo=None)
    def fake_http(url,*,timeout,user_agent):
     p=urlsplit(url); query=parse_qs(p.query)
     assert p.scheme=='https' and p.netloc=='api.twelvedata.com' and p.path=='/time_series' and query.get('interval')==['1day']
     calls.extend(query['symbol'])
     # Reach the real HTTP boundary, but do not make any request or market fact.
     return {'values':[]}
    def clock():return now
    def jump_before_http(original):
     def guarded(*args,**kwargs):
      nonlocal now, monotonic
      if case=='expiry_after_debit':
       now=until
      if case=='monotonic_after_debit':
       # Wall time is still before the approved bound, but the original anchored
       # elapsed budget has reached it (e.g. backward wall-clock correction).
       monotonic+=5.0
      return original(*args,**kwargs)
     return guarded
    consume=h.collectors.consume_twelve_credit
    after_consume=False
    external_ref=None
    competing_ledger=None
    def consume_then_cross_bound(*args, **kwargs):
     nonlocal now, monotonic, after_consume, external_ref, competing_ledger
     consume(*args, **kwargs)
     after_consume=True
     if case=='wall_before_debit_publish':
      now=until
     elif case=='elapsed_before_debit_publish':
      monotonic+=5.0
     elif case=='local_competing_change':
      ledger=repo/ledger_rel
      competing_ledger=ledger.read_bytes()+b'\n'
      ledger.write_bytes(competing_ledger)
     elif case=='remote_competing_change':
      parent=git(origin,'rev-parse','refs/heads/main')
      external_ref=git(origin,'commit-tree',parent+'^{tree}','-p',parent,'-m','Independent temporary Store write')
      git(origin,'update-ref','refs/heads/main',external_ref,parent)

    def verify_hosted_source(*args):
     nonlocal now
     if case=='late_handoff':
      # Upstream already passed its 1800s window at 12:30:01. Simulate
      # trusted source verification finishing at expiry, before native entry.
      now=until
    fixed=dict(schema_version=1,targets=targets)
    backend=hosted.HostedBackend(compute=compute,compute_sha=compute_sha,token='fake-fixture-token',
                                 key='fake-fixture-key',destination=repo)
    backend.grant=dict(execution_date=DAY,targets=targets,
                       valid_from=(until-timedelta(hours=1)).isoformat(timespec='seconds'),
                       valid_until=until.isoformat(timespec='seconds'))
    if case=='late_handoff':
     profile._window(backend.grant,clock())
    backend.op=profile.ClaimHandoff(old,op)
    params=dict(compute_sha=compute_sha,repo=repo,store_sha=op,execution_date=DAY,
                historical_cutoff=END,request_id=RUN,fixed_selector=fixed)
    a=n.r.admission()
    guard=a.guard_provider_request
    current=a.StorePublisher.assert_current
    native_git=a.git
    debit_pushes=0
    expected_parent_before_candidate=None
    prepared_debit_commit=None
    def watch_git(target,*args):
     nonlocal now, monotonic, debit_pushes, prepared_debit_commit, expected_parent_before_candidate
     only_new_budget = False
     if ('commit' in args and '--no-gpg-sign' in args
         and after_consume and prepared_debit_commit is None):
      staged=native_git(target,'diff','--cached','--name-only').splitlines()
      only_new_budget=staged==[ledger_rel]
      if only_new_budget:
       expected_parent_before_candidate=native_git(target,'rev-parse','HEAD')
     if args[:2]==('push','origin') and prepared_debit_commit and args[-1]==f'{prepared_debit_commit}:refs/heads/main':
      debit_pushes+=1
      result=native_git(target,*args)
      if case=='unconfirmed_push':
       raise a.AdmissionError('publication_failed')
      return result
     result=native_git(target,*args)
     if only_new_budget:
      prepared_debit_commit=native_git(target,'rev-parse','HEAD')
      if case=='wall_between_commit_and_push':
       now=until
      if case=='elapsed_between_commit_and_push':
       monotonic+=5.0
      if case=='local_competing_after_commit':
       assert Path(target)!=repo
       ledger=repo/ledger_rel
       nonlocal competing_ledger
       competing_ledger=ledger.read_bytes()+b'\n'
       ledger.write_bytes(competing_ledger)
     return result
    def after_writer_checks(writer,*args,**kwargs):
     nonlocal now
     ret=current(writer,*args,**kwargs)
     if case=='wall_during_writer_preflight' and after_consume:
      now=until
     return ret
    with patch.object(a,'git',side_effect=watch_git), patch.object(a.StorePublisher,'assert_current',new=after_writer_checks), patch.object(time,'monotonic',side_effect=lambda: monotonic), patch.object(h.collectors,'_http_json',side_effect=fake_http), patch.object(h.collectors,'consume_twelve_credit',side_effect=consume_then_cross_bound), patch.object(h.collectors,'datetime',FrozenDatetime), patch.object(h.rebaser,'datetime',FrozenDatetime), patch.object(n.r,'datetime',FrozenDatetime), patch.object(a,'guard_provider_request',side_effect=jump_before_http(guard)), patch.object(hosted,'_source',side_effect=verify_hosted_source), patch.object(hosted,'_environment',side_effect=lambda token: nullcontext()):
     if case=='legacy':
      outcome=n.maintain_append(h=h,compute=compute,config=cfg,universe=u,access=access,
                               key='fake-fixture-key',clock=clock,**params)
     else:
      # This fixture already exercised the real claim primitive before the
      # downstream clock mocks. Establish the same preclaim anchor explicitly;
      # the hosted descriptor fixture separately covers native preclaim + claim.
      native_wall=now
      now=min(now,until-timedelta(seconds=1800))
      backend._anchor_private_approval()
      monotonic+=(native_wall-now).total_seconds()
      now=native_wall
      outcome=backend.invoke_native(**params)
    head=git(origin,'rev-parse','refs/heads/main')
    files=git(origin,'diff','--name-only',op,head).splitlines()
    assert not any('/series/' in p for p in files), files
    credits=n._budget(repo,DAY,cfg)['credits']
    actual=(len(calls),credits,outcome['requests_attempted'],outcome['budget_debits_confirmed'],outcome['automatic_retry_allowed'])
    print(case,actual,flush=True)
    expect={'late_handoff':(0,0,0,0,False),'expiry_after_debit':(0,1,0,1,False),
            'monotonic_after_debit':(0,1,0,1,False),
            'wall_before_debit_publish':(0,prior_credits,0,0,False), 'elapsed_before_debit_publish':(0,0,0,0,False),
            'wall_during_writer_preflight':(0,0,0,0,False),
            'wall_between_commit_and_push':(0,0,0,0,False), 'elapsed_between_commit_and_push':(0,0,0,0,False),
            'local_competing_after_commit':(0,0,0,0,False), 'unconfirmed_push':(0,0,0,0,False),
            'local_competing_change':(0,0,0,0,False), 'remote_competing_change':(0,0,0,0,False),
            'legacy':(1,1,1,1,False),'pre_expiry':(1,1,1,1,False)}[case]
    assert actual==expect
    if case=='late_handoff':
     assert head==op
    blocked_before_durable=('wall_before_debit_publish','elapsed_before_debit_publish',
                             'wall_during_writer_preflight','local_competing_change','remote_competing_change',
                             'wall_between_commit_and_push','elapsed_between_commit_and_push','local_competing_after_commit')
    if case in blocked_before_durable:
     assert outcome['budget_debits_unconfirmed']==0
     if case.startswith(('wall_','elapsed_')):
      assert outcome['stop']['failure_code']=='deadline_exceeded'
      assert outcome['stop']['terminal_report_outcome']=='CONFIRMED'
     if case in ('local_competing_change','local_competing_after_commit'):
      assert outcome['stop']['failure_code']=='object_drift'
      assert outcome['stop']['terminal_report_outcome']=='UNCONFIRMED'
      assert (repo/ledger_rel).read_bytes()==competing_ledger
      assert git(repo,'rev-parse','HEAD')==head
     if case=='remote_competing_change':
      assert outcome['stop']['failure_code']=='object_drift'
      assert head==external_ref
     # Confirm no durable commit (including intermediate receipts) added a debit.
     for commit in git(origin,'rev-list',f'{op}..{head}').splitlines():
      if git(origin,'ls-tree',commit,'--',ledger_rel):
       ledger=json.loads(git(origin,'show',f'{commit}:{ledger_rel}'))
       assert ledger['credits']==prior_credits and ledger['minutes']==prior_minutes, ledger
    if case in ('wall_between_commit_and_push','elapsed_between_commit_and_push','local_competing_after_commit'):
     assert prepared_debit_commit and debit_pushes==0
     assert not git(origin,'tag','--list',f'market-data-read/{prepared_debit_commit}')
     assert [line for line in git(repo,'worktree','list','--porcelain').splitlines()
             if line.startswith('worktree ')]==[f'worktree {repo.resolve()}']
     assert not git(repo,'tag','--list',f'market-data-read/{prepared_debit_commit}')
     assert prepared_debit_commit not in git(origin,'rev-list',head).splitlines()
    if case=='unconfirmed_push':
     # The local bare origin accepted the physical push before the injected response loss.
     # Keep it, report precisely one unknown debit, never retry or write cleanup past it.
     assert prepared_debit_commit==head and debit_pushes==1
     assert git(repo,'rev-parse','HEAD')==expected_parent_before_candidate
     assert outcome['budget_debits_unconfirmed']==1
     assert outcome['stop']['publication_outcome']=='UNCONFIRMED'
     assert outcome['stop']['terminal_report_outcome']=='NOT_ATTEMPTED'
     assert json.loads(git(origin,'show',f'{head}:{ledger_rel}'))['credits']==1
    if case in ('expiry_after_debit','monotonic_after_debit'):
     assert head!=op and any(p.endswith('-twelve-data-budget.json') for p in files)
     assert outcome['stop']['failure_code']=='deadline_exceeded'


class PrivateTailExpiryTests(unittest.TestCase):
    def run_fixture(self, case):
        with tempfile.TemporaryDirectory(prefix='native-tail-expiry-') as temp:
            result = subprocess.run(
                [sys.executable, '-B', __file__, '--fixture', case, temp],
                check=False, capture_output=True, text=True, timeout=60,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        print(result.stdout, end='')

    def test_late_native_handoff(self):
        self.run_fixture('late_handoff')

    def test_durable_debit_before_wall_clock_expiry(self):
        self.run_fixture('expiry_after_debit')

    def test_elapsed_budget_survives_wall_clock_rewind(self):
        self.run_fixture('monotonic_after_debit')

    def test_expiry_after_local_mutation_before_durable_debit(self):
        for case in ('wall_before_debit_publish','elapsed_before_debit_publish','wall_during_writer_preflight'):
            with self.subTest(case=case):
                self.run_fixture(case)

    def test_prepared_debit_never_overwrites_competing_checkout_or_remote(self):
        for case in ('local_competing_change','remote_competing_change'):
            with self.subTest(case=case):
                self.run_fixture(case)

    def test_expiry_between_prepared_commit_and_actual_push(self):
        for case in ('wall_between_commit_and_push','elapsed_between_commit_and_push'):
            with self.subTest(case=case):
                self.run_fixture(case)

    def test_competing_checkout_after_commit_or_unconfirmed_push_is_preserved(self):
        for case in ('local_competing_after_commit','unconfirmed_push'):
            with self.subTest(case=case):
                self.run_fixture(case)

    def test_legacy_has_no_receipt_requirement(self):
        self.run_fixture('legacy')

    def test_approved_pre_expiry_path_reaches_provider_boundary(self):
        self.run_fixture('pre_expiry')


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == '--fixture':
        fixture(sys.argv[2], Path(sys.argv[3]).resolve())
    else:
        unittest.main(verbosity=2)
