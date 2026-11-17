"""Object-only, non-publishing adapter for the existing operational inventory.

The exact local HEAD plus a small bytes overlay is frozen in a temporary tree.
This is NOT remote authentication, durability, a complete asset validator, or a
new accounting ledger. In particular the existing malformed-session skip stays.
"""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile
from datetime import date
from build_market_data_inventory import build_inventory
from market_data_store import canonical_bytes, _datetime

WINDOWS={'open_15m':{'start':'09:30','end':'09:45'},'open_30m':{'start':'09:30','end':'10:00'},'open_60m':{'start':'09:30','end':'10:30'},'close':{'start':'15:45','end':'16:00'}}
PREFIX='data/market-data/'
class InventoryError(RuntimeError): pass


def git_read(repo,*args,input=None):
    result=subprocess.run(['git','-c','core.fsmonitor=false','-C',str(repo),*args],capture_output=True,input=input,
        env=dict(os.environ,GIT_ALLOW_PROTOCOL='',GIT_NO_LAZY_FETCH='1',GIT_TERMINAL_PROMPT='0'))
    if result.returncode:raise InventoryError('local Git object unavailable')
    return result.stdout


def _read_blob_batch(repo,oids):
    """Read exact local object IDs in one process; reject protocol or blob drift."""
    if any(not isinstance(oid,str) or not re.fullmatch('[0-9a-f]{40}',oid) for oid in oids):
        raise InventoryError('invalid blob object identity')
    if not oids:return []
    response=git_read(repo,'cat-file','--batch',input=('\n'.join(oids)+'\n').encode())
    offset=0;result=[]
    for oid in oids:
        end=response.find(b'\n',offset)
        if end<0:raise InventoryError('batch blob header missing')
        header=response[offset:end].split(b' ')
        if (len(header)!=3 or header[0]!=oid.encode() or header[1]!=b'blob'
                or not re.fullmatch(b'[0-9]+',header[2])):
            raise InventoryError('batch blob identity or type mismatch')
        size=int(header[2]);start=end+1;end=start+size
        if end>=len(response) or response[end:end+1]!=b'\n':
            raise InventoryError('batch blob content truncated')
        raw=response[start:end]
        if blob(raw)!=oid:raise InventoryError('batch blob content hash mismatch')
        result.append(raw);offset=end+1
    if offset!=len(response):raise InventoryError('batch blob trailing output')
    return result


# Bound each response body, apart from one indivisible oversized Git blob.
# The full frozen input bytes remain resident by the existing design.
BATCH_BYTES=8*1024*1024

def read_blobs(repo,oids):
    if any(not isinstance(oid,str) or not re.fullmatch('[0-9a-f]{40}',oid) for oid in oids):
        raise InventoryError('invalid blob object identity')
    if not oids:return []
    metadata=git_read(repo,'cat-file','--batch-check',input=('\n'.join(oids)+'\n').encode())
    lines=metadata.split(b'\n')
    if len(lines)!=len(oids)+1 or lines[-1]!=b'':
        raise InventoryError('batch metadata count mismatch')
    sizes=[]
    for oid,line in zip(oids,lines):
        header=line.split(b' ')
        if (len(header)!=3 or header[0]!=oid.encode() or header[1]!=b'blob'
                or not re.fullmatch(b'[0-9]+',header[2])):
            raise InventoryError('batch metadata identity or type mismatch')
        sizes.append(int(header[2]))
    result=[];start=0
    while start<len(oids):
        end=start;total=0
        while end<len(oids) and (end==start or total+sizes[end]<=BATCH_BYTES):
            total+=sizes[end];end+=1
        raws=_read_blob_batch(repo,oids[start:end])
        if [len(raw) for raw in raws]!=sizes[start:end]:
            raise InventoryError('batch metadata content size drift')
        result.extend(raws);start=end
    return result


def safe(relative):
    if (not isinstance(relative,str) or not relative or '\\' in relative or '\0' in relative
        or PurePosixPath(relative).is_absolute() or '..' in PurePosixPath(relative).parts
        or str(PurePosixPath(relative))!=relative):raise InventoryError('unsafe relative path')
    return relative


def blob(raw):
    return hashlib.sha1(f'blob {len(raw)}\0'.encode()+raw).hexdigest()


def document(raw):
    value=json.loads(raw)
    if not isinstance(value,dict):raise InventoryError('expected JSON object')
    return value


def build_candidate(*,repo,confirmed_head,day,stage,compute_sha,snapshot_path,overlays,capture_overlays=None):
    repo=Path(repo).resolve()
    if (not isinstance(confirmed_head,str) or not re.fullmatch('[0-9a-f]{40}',confirmed_head)
        or not isinstance(compute_sha,str) or not re.fullmatch('[0-9a-f]{40}',compute_sha)
        or stage not in WINDOWS):raise InventoryError('invalid candidate identity')
    try:
        if date.fromisoformat(day).isoformat()!=day:raise ValueError
    except (ValueError,TypeError):raise InventoryError('invalid day') from None
    def check_head():
        if git_read(repo,'rev-parse','HEAD').decode().strip()!=confirmed_head:raise InventoryError('local HEAD changed')
    check_head()
    no_snapshot=snapshot_path is None
    prefix=f'snapshots/{day[:7]}/{day}/{stage}/'
    if no_snapshot:
        if stage not in {'open_15m','open_30m','open_60m'} or overlays or capture_overlays:
            raise InventoryError('empty stage scope mismatch')
        overlay={};snap={};active={};latest=prefix+'latest.json'
    else:
        snapshot_path=safe(snapshot_path)
        prefix=f'snapshots/{day[:7]}/{day}/{stage}/'
        if not snapshot_path.startswith(prefix) or '/' in snapshot_path[len(prefix):] or snapshot_path.endswith('/latest.json'):
            raise InventoryError('snapshot scope mismatch')
        latest=prefix+'latest.json'
        if type(overlays) is not dict or not all(isinstance(p,str) and type(raw) is bytes for p,raw in overlays.items()):
            raise InventoryError('overlay must contain immutable bytes')
        overlay=dict(overlays)
        if not {snapshot_path,latest}<=overlay.keys():raise InventoryError('snapshot overlay incomplete')
    try:
        if not no_snapshot:
            snap=document(overlay[snapshot_path]);pointer=document(overlay[latest])
            sid=snap.get('snapshot_id')
            if not isinstance(sid,str) or not re.fullmatch('[A-Za-z0-9._-]+',sid) or sid in {'.','..'}:
                raise InventoryError('unsafe snapshot id')
            proof_path=f'proofs/deterministic-metrics/{day[:7]}/{day}/{stage}/{compute_sha}/{sid}.json'
            proof_latest=f'proofs/deterministic-metrics/{day[:7]}/{day}/{stage}/latest.json'
            allowed={snapshot_path,latest}|({proof_path,proof_latest} if stage!='open_15m' else set())
            if not set(overlay)<=allowed:raise InventoryError('unauthorized overlay')
            if (snap.get('trade_date')!=day or snap.get('stage')!=stage or snap.get('target_window')!=WINDOWS[stage]
                ):
                raise InventoryError('snapshot/latest binding mismatch')
            captures={} if capture_overlays is None else dict(capture_overlays)
            refs={ref['path'] for ref in snap.get('data_refs') or []}
            increments={'open_15m':('09:30','09:45'),'open_30m':('09:45','10:00'),'open_60m':('10:00','10:30'),'close':('15:45','16:00')}
            start,end=increments[stage]
            capture_prefix=f'sessions/{day[:7]}/{day}/regular/{start.replace(":","")}-{end.replace(":","")}/nasdaq_public_intraday/'
            for path,raw in captures.items():
                safe(path)
                if type(raw) is not bytes or path not in refs or not path.startswith(capture_prefix) or '/' in path[len(capture_prefix):]:
                    raise InventoryError('unreferenced or out-of-scope capture overlay')
                capture=document(raw)
                if (capture.get('provider')!='nasdaq_public_intraday' or capture.get('trade_date')!=day
                    or capture.get('session')!='regular' or capture.get('window')!={'start':start,'end':end}):
                    raise InventoryError('capture overlay identity mismatch')
            overlay.update(captures)
        files={}
        entries=git_read(repo,'ls-tree','-r','-z',confirmed_head,'--','data/market-data').split(b'\0')
        objects=[]
        for entry in filter(None,entries):
            meta,name=entry.split(b'\t',1);mode,kind,oid=meta.decode().split();full=name.decode()
            if mode not in {'100644','100755'} or kind!='blob':raise InventoryError('non-regular Git entry')
            if not full.startswith(PREFIX):raise InventoryError('unexpected Git path')
            rel=safe(full[len(PREFIX):]);objects.append((rel,oid))
        for (rel,oid),raw in zip(objects,read_blobs(repo,[oid for rel,oid in objects])):
            files[rel]=raw
        if no_snapshot:
            if any(p.startswith(prefix) for p in files):
                raise InventoryError('empty stage already has snapshot objects')
        else:
            # Match the existing writer, rather than treating overlays as authority.
            current_dt=_datetime(snap.get('generated_at'))
            if snapshot_path!=prefix+sid+'.json':raise InventoryError('snapshot identity path mismatch')
            if snapshot_path in files:
                if canonical_bytes(document(files[snapshot_path]))!=canonical_bytes(snap):
                    raise InventoryError('immutable snapshot conflict')
                # write_snapshot keeps the original file, including its exact bytes.
                overlay[snapshot_path]=files[snapshot_path]
            if proof_path in overlay and proof_path in files and overlay[proof_path]!=files[proof_path]:
                raise InventoryError('immutable proof conflict')
            unchanged_latest=False
            if latest in files:
                old=document(files[latest]);old_dt=_datetime(old.get('generated_at'))
                if current_dt==old_dt and old.get('snapshot_id')!=sid:
                    raise InventoryError('two snapshots share generated_at but differ in id')
                unchanged_latest=current_dt<=old_dt
                if unchanged_latest and overlay[latest]!=files[latest]:
                    raise InventoryError('latest must remain unchanged for non-advancing snapshot')
            active_path=safe(pointer.get('snapshot_path'))
            if not unchanged_latest and active_path!=snapshot_path:
                raise InventoryError('snapshot/latest binding mismatch')
            if unchanged_latest and active_path!=snapshot_path and ({proof_path,proof_latest}&overlay.keys()):
                raise InventoryError('proof overlay cannot replace active snapshot proof')
            for path in captures:
                if path in files:
                    if canonical_bytes(document(files[path]))!=canonical_bytes(document(captures[path])):
                        raise InventoryError('immutable capture conflict')
                    overlay[path]=files[path]
            files.update(overlay)
            if active_path not in files:raise InventoryError('snapshot/latest target missing')
            active=document(files[active_path])
            if (pointer.get('snapshot_blob_sha')!=blob(files[active_path])
                or pointer.get('snapshot_id')!=active.get('snapshot_id')
                or pointer.get('generated_at')!=active.get('generated_at')
                or active.get('trade_date')!=day or active.get('stage')!=stage):
                raise InventoryError('snapshot/latest binding mismatch')
        # Validate path-bearing fields BEFORE invoking existing permissive readers.
        for rel,raw in files.items():
            parts=PurePosixPath(rel).parts
            if len(parts)==5 and parts[0]=='snapshots' and parts[-1]=='latest.json':
                target=document(raw).get('snapshot_path')
                if target:safe(target)  # Missing targets retain the existing summary status.
            if len(parts)==5 and parts[:2]==('series','daily') and parts[-1]=='_index.json':
                for shard in document(raw).get('shards') or []:safe(shard['path'])
        for ref in (snap.get('data_refs') or [])+(active.get('data_refs') or []):
            path=safe(ref['path'])
            if path not in files or blob(files[path])!=ref.get('blob_sha'):raise InventoryError('snapshot reference blob mismatch')
            for fact in document(files[path]).get('qualified_facts') or []:
                if isinstance(fact,dict) and fact.get('symbol'):
                    symbol=fact['symbol']
                    if not isinstance(symbol,str) or not re.fullmatch('[A-Za-z0-9.^=-]+',symbol):
                        raise InventoryError('unsafe source symbol')
        with tempfile.TemporaryDirectory(prefix='fixed-inventory-offline-') as temporary:
            root=Path(temporary)/'store';root.mkdir()
            for rel,raw in files.items():
                target=root/safe(rel);target.parent.mkdir(parents=True,exist_ok=True)
                with target.open('xb') as out:out.write(raw)
            verified_proof=None
            if not no_snapshot and stage!='open_15m':
                from build_deterministic_metric_proof import verify_existing_proof
                try:
                    verified_proof,_=verify_existing_proof(root,trade_date=day,stage=stage,data_plane_commit_sha=compute_sha)
                except Exception as exc:raise InventoryError('existing proof invalid') from exc
            inventory=build_inventory(root)
            inventory_bytes=(json.dumps(inventory,ensure_ascii=False,sort_keys=True,separators=(',',':'))+'\n').encode()
        check_head()
        return dict(inventory_bytes=inventory_bytes,inventory_blob_sha=blob(inventory_bytes),
            inventory_path='collector-state/market-data-inventory.json',source_head=confirmed_head,
            input_blobs={p:blob(raw) for p,raw in sorted(files.items())},
            verified_proof_path=verified_proof,remote_authenticity_proven=False)
    except InventoryError:raise
    except (OSError,ValueError,TypeError,KeyError,AttributeError) as exc:
        raise InventoryError('frozen inventory input invalid') from exc
