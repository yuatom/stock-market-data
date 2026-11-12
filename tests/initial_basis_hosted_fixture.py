"""Synthetic single-admission emitter -> fresh clone -> GET-only read boundary.

No provider or actual service request. The protocol writers, immutable receipt,
private emitter, remote read adapter and final authority resolver are real code.
"""
import json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from test_initial_basis_delivery import fresh_case,read
from scripts import provider_basis_snapshot as B


def run(destination):
    root=Path(destination);root.mkdir(parents=True,exist_ok=True)
    case,store,transport=fresh_case(root)
    before=store.remote_ref('refs/heads/main')
    def forbidden(*args,**kwargs):raise AssertionError('initial read may not write')
    store.cas=store.publish=store.commit=forbidden
    result=read(case,store)
    assert not transport.pushes and store.remote_ref('refs/heads/main')==before
    grant=case['grant'];proof=grant['proof_ref'];other='data/market-data/fixtures/OTHER.json'
    return dict(binding=result['binding'],grant=grant,calendar=case['calendar'],
        repository=case['repository'],run=case['run'],jobs=case['jobs'],log=case['log'],
        now=case['now'],current_store_sha=result['current_store_sha'],
        proof=case['proof'],proof_path=proof['path'],proof_blob=proof['blob_sha'],
        other_ref=dict(path=other,blob_sha=B.blob_sha(store.read(before,other))),
        days=case['calendar'][grant['trade_date']]['previous_sessions'],
        store_path=str(store.repo),view=result['view'],provider_request_count=0,
        read_dispatch_count=0,remote_write_count=len(transport.pushes),
        clone_names=['initial-admission','fresh-reader'])


if __name__=='__main__':print(json.dumps(run(sys.argv[1])))
