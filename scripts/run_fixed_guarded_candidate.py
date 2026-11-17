"""Isolated production entrypoint candidate. Never run in offline tests.

Invocation JSON is emitted by the existing workflow resolver, not an attestation
service. Existing schedule, Close selection and provider retry contracts remain.
"""
import argparse,json,os,sys,time
from pathlib import Path
from datetime import datetime,timezone
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
import collect_market_data as entry
from fixed_live_owner_candidate import WorkflowFixedOwnerFactory
from fixed_stage_publisher_candidate import FixedStagePublisher,check_store_remote
from fixed_collection_publication import execute
from quiet_private_store import git_environment,STORE_URL
from admit_market_data_baseline import git

def publication_route(mode):
    return 'guarded' if mode in entry.runtime.FIXED_GUARDED_MODES else 'legacy'

def run(args,metadata,*,repo,compute,run_id,env,diagnostic=None):
    diagnostic={} if diagnostic is None else diagnostic
    diagnostic['stage']='argument_validation'
    mode=entry._arg_value(entry.normalize_mode_args(args),'--mode')
    if publication_route(mode)!='guarded':raise ValueError('not a guarded fixed mode')
    if metadata['mode']!=mode or metadata['trade_date']!=entry._arg_value(args,'--trade-date'):
        raise ValueError('resolved invocation differs from collection args')
    # Process-local environment in a dedicated CLI, not a shared thread context.
    original=dict(os.environ)
    try:
        diagnostic['stage']='git_environment'
        private=git_environment(env['MARKET_DATA_STORE_TOKEN'])
        os.environ.clear();os.environ.update(private)
        os.environ['MARKET_DATA_COMPUTE_SHA']=metadata['market_data_contract_sha']
        os.environ['MARKET_DATA_REQUEST_ID']=metadata.get('request_id') or ''
        if env.get('TWELVE_DATA_API_KEY'):os.environ['TWELVE_DATA_API_KEY']=env['TWELVE_DATA_API_KEY']
        diagnostic['stage']='input_validation'
        check_store_remote(repo)
        if git(repo,'rev-parse','HEAD')!=env['MARKET_DATA_STORE_INPUT_SHA']:raise ValueError('Store input drift')
        if git(compute,'rev-parse','HEAD')!=metadata['market_data_contract_sha']:raise ValueError('compute input drift')
        diagnostic['stage']='owner_initialization'
        factory=WorkflowFixedOwnerFactory(repo=repo,compute=compute,compute_sha=metadata['market_data_contract_sha'],
            run_id=run_id,invocation=dict(trigger=metadata['logical_trigger'],request_id=metadata.get('request_id'),
            transaction_id=metadata.get('transaction_id'),control_ref=metadata.get('control_ref')),
            clock=lambda:datetime.now(timezone.utc),deadline=time.monotonic()+600)
        diagnostic['stage']='universe_load'
        universe=entry.collection.load_collection_universe(entry._arg_value(args,'--universe'))
        diagnostic['stage']='publisher_initialization'
        publisher=FixedStagePublisher(factory=factory,day=metadata['trade_date'],eligible_universe=entry._live_close_universe(universe))
        def publish(stage_args):
            diagnostic['stage']='stage_publication'
            result=publisher(stage_args)
            diagnostic['stage']='collection'
            return result
        diagnostic['stage']='collection'
        return execute(args,owner_factory=factory,stage_publish=publish)
    finally:
        os.environ.clear();os.environ.update(original)

def main(argv=None):
    parser=argparse.ArgumentParser();parser.add_argument('--invocation',required=True);parser.add_argument('--failure-summary')
    known,args=parser.parse_known_args(argv)
    if args and args[0]=='--':args=args[1:]
    diagnostic={'stage':'invocation_load'}
    try:
        if known.failure_summary:Path(known.failure_summary).unlink(missing_ok=True)
        metadata=json.loads(Path(known.invocation).read_bytes())
        repo=Path(entry._arg_value(args,'--store-root')).resolve().parents[1]
        diagnostic['stage']='runner_execution'
        return run(args,metadata,repo=repo,compute=ROOT,run_id=os.environ['GITHUB_RUN_ID']+'-'+os.environ['GITHUB_RUN_ATTEMPT'],env=dict(os.environ),diagnostic=diagnostic)
    except Exception as exc:
        from fixed_failure_summary import summary
        value=summary(exc,diagnostic['stage'])
        if known.failure_summary:
            try:
                with open(known.failure_summary,'w',encoding='utf-8') as out:json.dump(value,out,sort_keys=True)
                Path(known.failure_summary).chmod(0o600)
            except OSError:pass
        print(json.dumps(value,sort_keys=True));return 2
if __name__=='__main__':raise SystemExit(main())
