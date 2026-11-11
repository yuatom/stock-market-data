from datetime import timedelta
from daily_basis_hosted_fixture import HostedChain

def test_completed_verification_before_delayed_dispatch_ack_remains_readable(tmp_path):
    chain=HostedChain(tmp_path)
    original=chain.request
    def delayed_ack(method,path,payload=None):
        reply=original(method,path,payload)
        if method=='POST':
            # The private verifier already emitted while its dispatch POST
            # acknowledgement was still in flight. No mutation of its evidence.
            chain.now+=timedelta(seconds=1)
        return reply
    chain.request=delayed_ack
    chain.run(0);chain.run(1)
    result=chain.read()
    assert result['capture_count']==result['dispatch_count']==2


def test_no_dispatch_when_reservation_roundtrip_exhausts_slot(tmp_path,monkeypatch):
    import pytest
    from scripts.daily_basis_updates import DailyUpdater
    from scripts.run_daily_basis_route import HostedDailyError
    from scripts import provider_basis_snapshot as B
    chain=HostedChain(tmp_path)
    original=DailyUpdater.reserve_verification
    def delayed_reservation(updater,candidate):
        ref=original(updater,candidate)
        # Durable intent succeeded, but network round trips consumed the last
        # remaining acquisition time before the external dispatch could begin.
        chain.now=B._stamp(chain.grant['slots'][0]['valid_until'])
        return ref
    monkeypatch.setattr(DailyUpdater,'reserve_verification',delayed_reservation)
    with pytest.raises(HostedDailyError):chain.run(0)
    assert len(chain.calls)==1
    assert len(chain.posts)==0,'dispatch started after finite slot closed'

    # The durable intent remains consumed, but no delivery mapping or extra
    # private verification run is created after the acquisition deadline.
    from daily_basis_hosted_fixture import git
    digest=B.sha(B.encode(chain.grant))
    assert git(chain.remote,'rev-parse',f'refs/heads/basis-daily-verification/{digest}/sep10')
    assert git(chain.remote,'for-each-ref','--format=%(refname)',f'refs/heads/basis-daily-deliveries/{digest}/sep10')==''


def test_delayed_dispatch_ack_reaches_unchanged_actual_dairy_sealer(tmp_path):
    import os,sys,subprocess
    from pathlib import Path
    import pytest
    consumer=os.environ.get('BASIS_CONSUMER_ROOT')
    if not consumer:pytest.skip('corrected cross-repository dairy checkout not supplied')
    consumer=Path(consumer).resolve();compute=Path(__file__).resolve().parents[1]
    wrapper=tmp_path/'delayed_hosted_producer.py'
    wrapper.write_text('''import sys,json
from datetime import timedelta
from pathlib import Path
sys.path.insert(0,COMPUTE_TESTS)
from daily_basis_hosted_fixture import HostedChain
chain=HostedChain(sys.argv[1]);original=chain.request
def delayed_ack(method,path,payload=None):
    reply=original(method,path,payload)
    if method=='POST':chain.now+=timedelta(seconds=1)
    return reply
chain.request=delayed_ack
chain.run(0);chain.run(1)
print(json.dumps(chain.read()))
'''.replace('COMPUTE_TESTS',repr(str(compute/'tests'))))
    check=tmp_path/'test_unchanged_actual_dairy.py'
    check.write_text('''import subprocess
from pathlib import Path
from test_daily_current_basis import test_canonical_fresh_clone_two_day_hosted_chain_seals_actual_dairy as canonical

def test_delayed_ack_keeps_authenticated_chronology_and_actual_seal(tmp_path,monkeypatch):
    original=subprocess.run
    def redirect_fixture(args,*a,**kw):
        if isinstance(args,(list,tuple)) and len(args)>1 and str(args[1]).endswith('/tests/daily_basis_hosted_fixture.py'):
            args=list(args);args[1]=WRAPPER
        return original(args,*a,**kw)
    monkeypatch.setattr(subprocess,'run',redirect_fixture)
    canonical(tmp_path)
'''.replace('WRAPPER',repr(str(wrapper))))
    env=dict(os.environ,DAILY_BASIS_COMPUTE_ROOT=str(compute),PYTEST_DISABLE_PLUGIN_AUTOLOAD='1')
    env['PYTHONPATH']=':'.join([str(consumer),str(consumer/'scripts'),str(consumer/'tests'),os.environ.get('PYTHONPATH','')])
    result=subprocess.run([sys.executable,'-m','pytest','-q',str(check)],cwd=consumer,env=env,capture_output=True,text=True,timeout=180)
    assert result.returncode==0,result.stdout+result.stderr
    assert '1 passed' in result.stdout
