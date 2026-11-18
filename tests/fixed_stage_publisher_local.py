"""Test-only file-protocol transport around the production stage algorithm."""
import json,os,subprocess,sys
from pathlib import Path
from admit_market_data_baseline import StorePublisher
from fixed_intraday_offline import require_local_bare
from fixed_stage_publisher_candidate import FixedStagePublisher

class LocalOnlyStorePublisher(StorePublisher):
    def _git(self,repo,*args):
        if args and args[0] in {'push','fetch','ls-remote'}:
            require_local_bare(repo)
        # A per-child protocol allowlist closes the check/use gap for network
        # schemes even if another process rewrites remote config concurrently.
        env=dict(os.environ,GIT_ALLOW_PROTOCOL='file',GIT_TERMINAL_PROMPT='0')
        env.pop('TWELVE_DATA_API_KEY',None)
        result=subprocess.run(['git','-C',str(repo),*args],capture_output=True,text=True,
            timeout=90,env=env)
        if result.returncode:raise ValueError('local-only Git command failed')
        return result.stdout.strip()

class LocalStagePublisher(FixedStagePublisher):
    publisher_type=LocalOnlyStorePublisher
    def _check_remote(self):require_local_bare(self.repo)
    def _verify(self,repo,sha):
        require_local_bare(repo)
        # Retention contains its own fetch/tag pushes. Run the existing verifier
        # unchanged in a protocol-restricted child, never alter process-wide env.
        code="import json,sys; from fixed_intraday_offline import require_local_bare; from pathlib import Path; from verify_store_publication import verify_publication; r=Path(sys.argv[1]); require_local_bare(r); print(json.dumps(verify_publication(r,sys.argv[2],required_manifest=sys.argv[3])))"
        env=dict(os.environ,GIT_ALLOW_PROTOCOL='file',GIT_TERMINAL_PROMPT='0',
            PYTHONPATH=os.pathsep.join([str(Path(__file__).resolve().parent),str(Path(__file__).resolve().parents[1]/'scripts')]))
        env.pop('TWELVE_DATA_API_KEY',None)
        result=subprocess.run([sys.executable,'-c',code,str(repo),sha,self.last_manifest],
            capture_output=True,text=True,env=env,timeout=150)
        if result.returncode:raise ValueError('local-only retention failed')
        return json.loads(result.stdout)
