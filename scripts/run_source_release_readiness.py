"""Manual, single fixed release GET. No provider, dispatch, runner or Store writer."""
import argparse,base64,json,os,urllib.error
from pathlib import Path
from scripts import provider_basis_snapshot as B
from scripts.run_daily_basis_route import ReportReleaseReader,REPORT_RELEASE_PATH
from scripts.run_private_authority_route import _quiet,_source,_environment
COMPUTE='yuatom/stock-market-data'
WORKFLOW='.github/workflows/source-release-readiness.yml'
OWNER=10399225
STAGES=frozenset({'INPUT','SOURCE','CREDENTIAL','REPORT_READ','REPORT_CONTRACT'})
STOP='SOURCE_RELEASE_READINESS_STOPPED_NO_RETRY'
class ReadinessError(RuntimeError):
    def __init__(self,stage,error):
        self.stage=stage if stage in STAGES else 'INPUT'
        self.http_status=error.code if isinstance(error,urllib.error.HTTPError) and type(error.code) is int and 100<=error.code<=599 else None
        self.category='HTTP_ERROR' if self.http_status is not None else 'TIMEOUT' if isinstance(error,TimeoutError) else 'NETWORK_ERROR' if isinstance(error,urllib.error.URLError) else 'CONTRACT_OR_INPUT'
        super().__init__(STOP)
def need(value):
    if not value:raise ValueError(STOP)
def run_probe(*,reader,compute_sha,consumer_sha,env,source_check):
    stage='INPUT'
    try:
        B._hex(compute_sha);B._hex(consumer_sha)
        expected=dict(GITHUB_ACTIONS='true',GITHUB_EVENT_NAME='workflow_dispatch',GITHUB_RUN_ATTEMPT='1',GITHUB_REPOSITORY=COMPUTE,GITHUB_REF='refs/heads/main',GITHUB_SHA=compute_sha,GITHUB_ACTOR='yuatom',GITHUB_TRIGGERING_ACTOR='yuatom',GITHUB_ACTOR_ID=str(OWNER),GITHUB_REPOSITORY_OWNER_ID=str(OWNER),GITHUB_JOB='source_readiness',GITHUB_WORKFLOW_REF=COMPUTE+'/'+WORKFLOW+'@refs/heads/main')
        need(all(env.get(k)==v for k,v in expected.items()))
        stage='SOURCE';source_check()
        stage='REPORT_READ';release=reader.request('GET',REPORT_RELEASE_PATH)
        stage='REPORT_CONTRACT'
        need(type(release) is dict and release.get('type')=='file' and release.get('path')=='state/runtime-release.json' and release.get('encoding')=='base64')
        raw=base64.b64decode(release['content'].replace('\n',''),validate=True)
        need(B.blob_sha(raw)==release['sha'] and B._parse(raw,canonical=False)['repository_commit_sha']==consumer_sha)
        return dict(status='SOURCE_RELEASE_READINESS_VERIFIED',provider_requests=0,automatic_retry_allowed=False)
    except Exception as exc:raise ReadinessError(stage,exc) from None

def failure_result(error):
    safe=error if isinstance(error,ReadinessError) else ReadinessError('INPUT',error)
    return dict(status=STOP,failure_stage=safe.stage if safe.stage in STAGES else 'INPUT',http_status=safe.http_status if type(safe.http_status) is int and 100<=safe.http_status<=599 else None,error_category=safe.category if safe.category in {'HTTP_ERROR','TIMEOUT','NETWORK_ERROR','CONTRACT_OR_INPUT'} else 'CONTRACT_OR_INPUT',provider_requests=0,automatic_retry_allowed=False)

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--expected-compute-sha',required=True);parser.add_argument('--expected-consumer-sha',required=True)
    args=parser.parse_args()
    try:
        with _quiet():
            try:reader=ReportReleaseReader(os.environ.get('MARKET_DATA_SOURCE_TOKEN',''))
            except Exception as exc:raise ReadinessError('CREDENTIAL',exc) from None
            def source_check():
                with _environment():_source(Path(__file__).resolve().parents[1],args.expected_compute_sha)
            result=run_probe(reader=reader,compute_sha=args.expected_compute_sha,consumer_sha=args.expected_consumer_sha,env=dict(os.environ),source_check=source_check)
        print(json.dumps(result));return 0
    except Exception as exc:print(json.dumps(failure_result(exc)));return 2
if __name__=='__main__':raise SystemExit(main())
