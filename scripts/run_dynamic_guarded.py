#!/usr/bin/env python3
"""Guarded dynamic runtime entrypoint; no retry or legacy outer publisher."""
import argparse,json,os,re,time
from datetime import datetime,timezone
from pathlib import Path
import admit_market_data_baseline as base
import dynamic_runtime_authority as authority
from dynamic_daily_owner import DynamicDailyOwner,DynamicPublicationUnknown,encode,blob
import verify_store_publication as durable
from build_market_data_inventory import build_inventory

def origin(repo,fullname):
    value=base.git(repo,'remote','get-url','origin')
    authority.require(value in ('https://github.com/'+fullname,'https://github.com/'+fullname+'.git','git@github.com:'+fullname+'.git'))
    authority.require(base.git(repo,'rev-parse','--is-shallow-repository')=='false')

def finalize(writer,result):
    """Publish only this request's post-daily output closure from last durable head."""
    try:return _finalize(writer,result)
    except Exception:writer.abort()

def _finalize(writer,result):
    if writer.failed or writer.active is not None:raise ValueError('owner not closed')
    root=writer.repo/'data/market-data';day=writer.day;stage='discovery_'+writer.request['stage']
    manifest_rel=result['publication_manifest_path'];manifest_path=root/manifest_rel
    changed=set(filter(None,base.git(writer.repo,'diff','--name-only','-z',writer.head,'--').split('\0')))
    changed.update(filter(None,base.git(writer.repo,'ls-files','--others','--exclude-standard','-z').split('\0')))
    required={x['path'] for x in result['required_store_outputs']}
    allowed={manifest_rel,result['result_state_path'],writer.budget_relative.removeprefix('data/market-data/')}
    if result.get('snapshot_written'):allowed.add(result['snapshot_path'])
    # Captures are admitted through the actual snapshot's exact blob references.
    if result.get('snapshot_written'):
        rel=result['snapshot_path']
        prefix=f'snapshots/{day[:7]}/{day}/{stage}/'
        if not rel.startswith(prefix) or '/' in rel[len(prefix):]:raise ValueError('snapshot scope')
        snapshot=json.loads((root/rel).read_bytes())
        if snapshot['trade_date']!=day or snapshot['stage']!=stage:raise ValueError('snapshot identity')
        for ref in snapshot['data_refs']:
            path=ref['path'];full=root/path
            if not full.resolve().is_relative_to(root.resolve()) or blob(full.read_bytes())!=ref['blob_sha']:raise ValueError('snapshot reference drift')
            required.add(path)
            if 'data/market-data/'+path in changed and path.startswith(f'sessions/{day[:7]}/{day}/regular/'):
                capture=json.loads(full.read_bytes())
                if capture['trade_date']!=day or capture['session']!='regular' or not capture['capture_id'].startswith(stage+'-'):raise ValueError('capture identity')
                if not {x['symbol'] for x in capture['qualified_facts']}<=set(writer.request['candidate_symbols']):raise ValueError('capture scope')
                allowed.add(path)
        latest=prefix+'latest.json'
        if (root/latest).exists():
            if 'data/market-data/'+latest in changed:
                pointer=json.loads((root/latest).read_bytes())
                if pointer.get('snapshot_path')!=rel or pointer.get('snapshot_blob_sha')!=blob((root/rel).read_bytes()):raise ValueError('latest pointer drift')
            required.add(latest);allowed.add(latest)
    close=f'collector-state/{day[:7]}/{day}-close-missing.json'
    if writer.request['stage']=='close' and (root/close).exists():
        doc=json.loads((root/close).read_bytes())
        if doc.get('last_mode')==stage and doc.get('trade_date')==day:required.add(close);allowed.add(close)
    inventory='collector-state/market-data-inventory.json'
    # Validate unknown local changes before generating any further object.
    changed=set(filter(None,base.git(writer.repo,'diff','--name-only','-z',writer.head,'--').split('\0')))
    changed.update(filter(None,base.git(writer.repo,'ls-files','--others','--exclude-standard','-z').split('\0')))
    if not changed<={'data/market-data/'+p for p in allowed}:raise ValueError('unexpected final changes')
    (root/inventory).write_bytes(encode(build_inventory(root)))
    required.add(inventory);allowed.add(inventory)
    manifest=json.loads(manifest_path.read_bytes())
    manifest['required_outputs']=[dict(path=p,blob_sha=blob((root/p).read_bytes())) for p in sorted(required)]
    manifest_path.write_bytes(encode(manifest))
    # Previously durable daily objects are verified but not re-added/re-pushed.
    paths=sorted(changed|{'data/market-data/'+inventory,'data/market-data/'+manifest_rel})
    writer.allowed.update(paths)
    writer.verify=lambda repo,sha:durable.verify_publication(repo,sha,required_manifest='data/market-data/'+manifest_rel)
    sha=writer.publish(paths)
    result.update(store_commit_sha=sha,publication_status='DURABILITY_VERIFIED',inventory_path=inventory)
    return result

def main(argv=None):
    p=argparse.ArgumentParser();p.add_argument('--control',required=True);p.add_argument('--control-ref',required=True);p.add_argument('--store',required=True)
    a=p.parse_args(argv);compute=Path(__file__).resolve().parents[1];control=Path(a.control).resolve();store=Path(a.store).resolve()
    sha=base.git(compute,'rev-parse','HEAD')
    for repo,name in ((compute,authority.COMPUTE),(control,authority.COMPUTE),(store,authority.STORE)):
        origin(repo,name)
        authority.require(not base.git(repo,'status','--porcelain') and not base.git(repo,'ls-files','--others'))
    authority.require(base.git(control,'rev-parse','HEAD')==a.control_ref)
    attestation=authority.attest(os.environ,control_sha=a.control_ref,compute_sha=sha)
    current=base.git(compute,'ls-remote','origin','refs/heads/main').split()
    authority.require(current==[sha,'refs/heads/main'])
    request_blob=base.git(control,'rev-parse',a.control_ref+':requests/dynamic-candidate-request.json')
    from zoneinfo import ZoneInfo
    day=datetime.now(ZoneInfo('America/New_York')).date().isoformat()
    writer=DynamicDailyOwner(store,base.git(store,'rev-parse','HEAD'),day,str(attestation['run_id'])+'-1',compute=compute,compute_sha=sha,control=control,request_commit=a.control_ref,request_blob=request_blob,clock=lambda:datetime.now(timezone.utc),deadline=time.monotonic()+420)
    from collect_dynamic_candidates import collect_request
    try:
        result=collect_request(request=writer.request,store_root=store/'data/market-data',store_config_path=compute/'config/market-data-store.yaml',access_path=compute/'config/market-data-collector-access.yaml',dynamic_config_path=compute/'config/dynamic-candidate-collection.yaml',durable_owner=writer)
        result=finalize(writer,result)
    except Exception:
        writer.abort() # No saved output or alternate publisher after uncertain execution.
    print(json.dumps(result,sort_keys=True))
    return 2 if result.get('status')=='STOPPED' else 0
if __name__=='__main__':raise SystemExit(main())
