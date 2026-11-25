"""Review-only production stage callback, not activated by this file.

Uses the existing authenticated Store remote and existing exact publisher.
Entrypoint must install quiet_private_store's child Git environment first.
"""
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from admit_market_data_baseline import StorePublisher,git
import json
import collect_market_data as entry
from fixed_finalizer_offline import build_candidate
from quiet_private_store import STORE_URL
from verify_store_publication import verify_publication

def check_store_remote(repo):
    for args in [('remote','get-url','--all','origin'),('remote','get-url','--push','--all','origin')]:
        if git(repo,*args).splitlines()!=[STORE_URL]:raise ValueError('Store remote mismatch')

class FixedStagePublisher:
    publisher_type=StorePublisher
    def _check_remote(self):check_store_remote(self.repo)
    def _verify(self,repo,sha):
        self._check_remote()
        return verify_publication(repo,sha,required_manifest=self.last_manifest)
    def __init__(self,*,factory,day,eligible_universe=None):
        self.factory=factory;self.day=day;self.repo=factory.repo
        self.eligible_universe=eligible_universe;self.failed=False;self.last_manifest=None
        self._check_remote()
        self.publisher=self.publisher_type(self.repo,git(self.repo,'rev-parse','HEAD'),day,factory.run_id,verify=self._verify)
        self.seen_owner=None
    def __call__(self,args):
        if self.failed:raise ValueError('stage publisher stopped after unknown outcome')
        try:return self._publish(args)
        except Exception:
            self.failed=True
            raise
    def _publish(self,args):
        self._check_remote()
        args=entry.normalize_mode_args(args);mode=entry._arg_value(args,'--mode')
        stage='close' if mode in {'close','close_retry','close_final'} else mode
        if mode not in entry.runtime.FIXED_GUARDED_MODES or entry._arg_value(args,'--trade-date')!=self.day:
            raise ValueError('stage publication scope mismatch')
        if Path(entry._arg_value(args,'--store-root')).resolve()!=self.repo/'data/market-data':raise ValueError('Store root mismatch')
        owner=self.factory.latest_owner
        fresh=owner is not None and owner is not self.seen_owner
        start=self.publisher.head
        head=git(self.repo,'rev-parse','HEAD')
        if fresh:
            if owner.failed or owner.active or owner.base!=start or owner.head!=head or owner.request['collection_mode']!=mode:
                raise ValueError('stage owner incomplete or mismatched')
        elif head!=start:raise ValueError('unowned HEAD advance')
        if self.publisher.remote_head()!=head:raise ValueError('remote HEAD drift')
        root=self.repo/'data/market-data'
        pointer=root/f'snapshots/{self.day[:7]}/{self.day}/{stage}/latest.json'
        latest=json.loads(pointer.read_bytes()) if pointer.exists() else None
        manifest=build_candidate(repo=self.repo,start_sha=start,confirmed_head=head,day=self.day,stage=stage,
            request_id=self.factory.run_id,run_id=self.factory.run_id,compute_sha=self.factory.compute_sha,
            snapshot_path=latest['snapshot_path'] if latest else None,profile='fixed_live',business_request_id=self.factory.invocation.get('request_id'),
            collection_mode=mode,nothing_missing=not fresh and mode in {'close_retry','close_final'},eligible_universe=self.eligible_universe,previous_manifest=self.last_manifest)
        self.last_manifest=manifest.relative_to(self.repo).as_posix()
        outputs=json.loads(manifest.read_bytes())['required_outputs']
        paths=['data/market-data/'+r['path'] for r in outputs]+[self.last_manifest]
        self.publisher.allowed=set(paths);self.publisher.head=head
        result=self.publisher.publish(paths)
        self.publisher.base=result;self.seen_owner=owner
        return result
