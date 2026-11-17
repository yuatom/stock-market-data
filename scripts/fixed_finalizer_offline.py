"""Zero-network output-closure candidate, not a publisher or authority grant.

Only local Git object reads. Includes all this invocation's committed attempt
outputs rather than relying on the remaining worktree diff. Does not claim
remote durability or activate a production workflow. Inventory and proof are
validated against one frozen local object view before output bytes are written.
"""
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from dynamic_daily_owner import blob, encode

SHA = re.compile(r'[0-9a-f]{40}\Z')
PREFIX = 'data/market-data/'
WINDOWS = {'open_15m': {'start':'09:30','end':'09:45'},
           'open_30m': {'start':'09:30','end':'10:00'},
           'open_60m': {'start':'09:30','end':'10:30'},
           'close': {'start':'15:45','end':'16:00'}}
class FinalizerError(RuntimeError): pass


def local_git(repo, *args):
    # No caller-controlled command, no remote/ref resolution or publication.
    result = subprocess.run(['git','-c','core.fsmonitor=false','-C',str(repo),*args],
                            capture_output=True, check=False, env=dict(os.environ, GIT_ALLOW_PROTOCOL='',
                            GIT_NO_LAZY_FETCH='1', GIT_TERMINAL_PROMPT='0'))
    if result.returncode:
        raise FinalizerError('local Git identity unavailable')
    return result.stdout


def safe_path(root, relative):
    if (not isinstance(relative,str) or not relative or '\\' in relative or
            PurePosixPath(relative).is_absolute() or '..' in PurePosixPath(relative).parts
            or str(PurePosixPath(relative)) != relative):
        raise FinalizerError('unsafe relative output')
    path = root / relative
    for part in (path,*path.parents):
        if part == root: break
        if part.is_symlink(): raise FinalizerError('symlink output')
    if not path.resolve().is_relative_to(root.resolve()): raise FinalizerError('output escaped root')
    return path


def build_candidate(*, repo, start_sha, confirmed_head, day, stage, request_id,
                    transaction_id=None, compute_sha, snapshot_path, profile="offline", business_request_id=None, run_id=None,
                    collection_mode=None, nothing_missing=False, eligible_universe=None, previous_manifest=None):
    live=profile=='fixed_live'
    no_snapshot=snapshot_path is None
    if no_snapshot and (not live or stage not in {'open_15m','open_30m','open_60m'} or nothing_missing):
        raise FinalizerError('empty stage scope mismatch')
    attempt_key=run_id if live else transaction_id
    if profile not in {'offline','fixed_live'}:raise FinalizerError('unknown attempt profile')
    if live:
        if not re.fullmatch(r'[0-9]+-[0-9]+',str(attempt_key)):raise FinalizerError('invalid live run identity')
        expected_stage='close' if collection_mode in {'close','close_retry','close_final'} else collection_mode
        if expected_stage!=stage:raise FinalizerError('collection mode/stage mismatch')
    elif stage=='close' or nothing_missing:raise FinalizerError('offline profile scope mismatch')
    expected_mode='fixed_intraday_attempt' if live else 'fixed_intraday_attempt_offline'
    expected_business=business_request_id if live else transaction_id
    repo=Path(repo).resolve();root=repo/'data/market-data'
    if root.is_symlink() or (repo/'data').is_symlink(): raise FinalizerError('symlink Store root')
    if (not all(SHA.fullmatch(str(x)) for x in (start_sha,confirmed_head,compute_sha))
            or stage not in WINDOWS or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',day)
            or not isinstance(request_id,str) or not 1<=len(request_id)<=160
            or not isinstance(attempt_key,str) or not 1<=len(attempt_key)<=200):
        raise FinalizerError('candidate identity invalid')
    if local_git(repo,'rev-parse','HEAD').decode().strip()!=confirmed_head:
        raise FinalizerError('confirmed local HEAD changed')
    local_git(repo,'merge-base','--is-ancestor',start_sha,confirmed_head)
    # Include paths touched at ANY commit, including create-then-delete outputs.
    revisions=local_git(repo,'rev-list','--reverse',f'{start_sha}..{confirmed_head}').decode().split()
    committed=set()
    closure_manifests=[]
    budget=f'{PREFIX}collector-state/{day[:7]}/{day}-twelve-data-budget.json'
    for revision in revisions:
        parents=local_git(repo,'rev-list','--parents','-n','1',revision).decode().split()
        if len(parents)!=2: raise FinalizerError('offline closure requires linear local history')
        touched=set(filter(None,local_git(repo,'diff','--name-only','-z',parents[1],revision).decode().split('\0')))
        committed.update(touched)
        if budget in touched:
            ledger=json.loads(local_git(repo,'show',f'{revision}:{budget}'))
            if (not isinstance(ledger.get('request_id'),str) or not ledger['request_id'] or ledger.get('trade_date')!=day
                    or ledger.get('stage')!=expected_mode or (live and ledger.get('request_id')!=attempt_key)):
                raise FinalizerError('historical closure identity mismatch')
            refs=ledger.get('required_outputs')
            if not isinstance(refs,list): raise FinalizerError('historical closure unavailable')
            closure_manifests.append(refs)
            committed.update(PREFIX+ref['path'] for ref in refs)
    changed=set(filter(None,local_git(repo,'diff','--name-only','-z',confirmed_head).decode().split('\0')))
    changed.update(filter(None,local_git(repo,'ls-files','--others','--exclude-standard','-z').decode().split('\0')))
    inventory='collector-state/market-data-inventory.json'
    safe_path(root,inventory)  # Reject a preexisting symlink before any write.
    snapshot_prefix=f'snapshots/{day[:7]}/{day}/{stage}/'
    if no_snapshot:
        latest=snapshot_prefix+'latest.json';allowed_new=set();new_captures=set();state_outputs=set()
    else:
        if not snapshot_path.startswith(snapshot_prefix) or '/' in snapshot_path[len(snapshot_prefix):]:
            raise FinalizerError('snapshot scope mismatch')
        latest=snapshot_prefix+'latest.json'
        allowed_new={snapshot_path,latest}
        close_state=f'collector-state/{day[:7]}/{day}-close-missing.json'
        state_outputs={close_state} if live and stage=='close' and PREFIX+close_state in changed else set()
        allowed_new.update(state_outputs)
        new_captures=set()
        if live:
            try:
                refs=json.loads(safe_path(root,snapshot_path).read_bytes())['data_refs']
                for ref in refs:
                    relative=ref['path']
                    if PREFIX+relative in changed and relative.startswith(f'sessions/{day[:7]}/{day}/regular/'):
                        new_captures.add(relative)
            except (OSError,KeyError,TypeError,ValueError) as exc:raise FinalizerError('live snapshot unavailable') from exc
            allowed_new.update(new_captures)
        if stage!='open_15m':
            # Select permitted paths only. Actual proof validation happens in the
            # frozen inventory view, not against an independently changing worktree.
            try:
                sid=json.loads(safe_path(root,snapshot_path).read_bytes())['snapshot_id']
                if not isinstance(sid,str) or not re.fullmatch(r'[A-Za-z0-9._-]+',sid) or sid in {'.','..'}:
                    raise ValueError('invalid snapshot identity')
                proof=f'proofs/deterministic-metrics/{day[:7]}/{day}/{stage}/{compute_sha}/{sid}.json'
                proof_latest=f'proofs/deterministic-metrics/{day[:7]}/{day}/{stage}/latest.json'
                if not safe_path(root,proof).is_file() or not safe_path(root,proof_latest).is_file():
                    raise ValueError('proof files missing')
            except Exception as exc:
                raise FinalizerError('existing deterministic proof invalid') from exc
            allowed_new.update({proof,proof_latest})
    raw_cache={}
    for full in committed|changed:
        if not full.startswith(PREFIX): raise FinalizerError('unexpected output outside Store')
        relative=full[len(PREFIX):];p=safe_path(root,relative)
        if not p.is_file(): raise FinalizerError('deleted or non-file output')
        raw=p.read_bytes();raw_cache[relative]=raw
        if full in committed:
            expected=local_git(repo,'show',f'{confirmed_head}:{full}')
            if blob(raw)!=blob(expected): raise FinalizerError('committed output changed locally')
            if not (re.fullmatch(r'collector-state/fixed-intraday-attempt-[0-9a-f]{64}(?:-terminal)?\.json',relative)
                or re.fullmatch(rf'collector-state/{day[:7]}/fixed-intraday-[A-Z0-9.-]+-[0-9a-f]{{64}}\.raw',relative)
                or relative==f'collector-state/{day[:7]}/{day}-twelve-data-budget.json'
                or relative.startswith(f'sessions/{day[:7]}/{day}/regular/')):
                raise FinalizerError('unexpected committed output scope')
        elif relative not in allowed_new:
            raise FinalizerError('unexpected uncommitted output')
    for relative, raw in raw_cache.items():
        if relative.endswith('.raw') and hashlib.sha256(raw).hexdigest()!=relative[-68:-4]:
            raise FinalizerError('raw content hash mismatch')
    for refs in closure_manifests:
        for ref in refs:
            if ref['path'] not in raw_cache or blob(raw_cache[ref['path']])!=ref['blob_sha']:
                raise FinalizerError('historical closure blob mismatch')
    claims=[p for p in raw_cache if re.fullmatch(r'collector-state/fixed-intraday-attempt-[0-9a-f]{64}\.json',p)]
    if not claims:
        if not live:raise FinalizerError('no committed attempt closure')
        if budget in committed|changed or any(p.endswith('.raw') or 'fixed-intraday-attempt-' in p for p in raw_cache):
            raise FinalizerError('accounting evidence without attempt')
    bound_twelve_captures=set()
    for claim_path in claims:
        claim=json.loads(raw_cache[claim_path]);terminal_path=claim_path[:-5]+'-terminal.json'
        if (claim.get('business_request_id')!=expected_business or claim.get('execution_day')!=day
                or claim.get('mode')!=expected_mode or (live and claim.get('run_id')!=attempt_key)
                or claim.get('compute_sha')!=compute_sha
                or claim.get('intraday_scope',{}).get('stage')!=stage
                or terminal_path not in raw_cache):
            raise FinalizerError('unknown or mismatched attempt')
        identity=(attempt_key+'\0'+str(claim.get('symbol'))).encode()+b'\0'+encode(claim.get('intraday_scope'))
        if claim_path != 'collector-state/fixed-intraday-attempt-'+hashlib.sha256(identity).hexdigest()+'.json':
            raise FinalizerError('claim identity hash mismatch')
        terminal=json.loads(raw_cache[terminal_path])
        if (terminal.get('symbol')!=claim.get('symbol') or terminal.get('intraday_scope')!=claim.get('intraday_scope')
                or terminal.get('status') not in {'SELECTED_TWELVE','UNSELECTED_VALID','REJECTED'}
                or terminal.get('reservation') not in {'DEBIT_CONFIRMED','DEBIT_CONFIRMED_NO_HTTP'}):
            raise FinalizerError('terminal binding invalid')
        # Reuse the immutable historical budget manifest to bind terminal+claim+raw.
        raw_prefix=f'collector-state/{day[:7]}/fixed-intraday-{claim["symbol"]}-'
        capture=terminal.get('selected_capture')
        if no_snapshot and (capture or terminal.get('status')=='SELECTED_TWELVE'):
            raise FinalizerError('empty stage contains qualified capture')
        bound=False
        for refs in closure_manifests:
            paths={ref['path'] for ref in refs}
            raws={p for p in paths if p.startswith(raw_prefix) and p.endswith('.raw')}
            if claim_path in paths and terminal_path in paths and len(raws)==1 and (not capture or capture in paths):
                bound=True
        if not bound: raise FinalizerError('terminal raw closure binding unavailable')
        if capture and capture not in raw_cache: raise FinalizerError('terminal capture omitted')
        if terminal.get('status')=='SELECTED_TWELVE' and not capture:
            raise FinalizerError('selected capture missing')
        if capture:
            doc=json.loads(raw_cache[capture]);provider=doc.get('provider');scope=claim['intraday_scope']
            expected_prefix=f"sessions/{day[:7]}/{day}/regular/{scope['start'].replace(':','')}-{scope['end'].replace(':','')}/{provider}/"
            if (provider not in {'nasdaq_public_intraday','twelve_data_basic'} or not capture.startswith(expected_prefix)
                or '/' in capture[len(expected_prefix):] or doc.get('trade_date')!=day or doc.get('session')!='regular'
                or doc.get('window')!={'start':scope['start'],'end':scope['end']}
                or not doc.get('qualified_facts') or any(f.get('symbol')!=claim['symbol'] for f in doc['qualified_facts'])
                or (terminal.get('status')=='SELECTED_TWELVE')!=(provider=='twelve_data_basic')):
                raise FinalizerError('terminal capture binding invalid')
            if provider=='twelve_data_basic':bound_twelve_captures.add(capture)
    # Check ALL capture paths touched in the invocation history or worktree,
    # before adding untouched inherited refs. A committed file is not by itself
    # evidence of a reserved/closed provider attempt.
    for path,raw in raw_cache.items():
        if path.startswith(f'sessions/{day[:7]}/{day}/regular/'):
            provider=json.loads(raw).get('provider')
            if provider=='twelve_data_basic' or '/twelve_data_basic/' in path:
                if provider!='twelve_data_basic' or '/twelve_data_basic/' not in path or path not in bound_twelve_captures:
                    raise FinalizerError('unbound Twelve capture in invocation outputs')
    if not no_snapshot:
        try:
            snap_raw=safe_path(root,snapshot_path).read_bytes();snap=json.loads(snap_raw)
            pointer=json.loads(safe_path(root,latest).read_bytes())
            if (snap.get('stage')!=stage or snap.get('trade_date')!=day or snap.get('target_window')!=WINDOWS[stage]
                    or pointer.get('snapshot_path')!=snapshot_path or pointer.get('snapshot_blob_sha')!=blob(snap_raw)
                    or pointer.get('snapshot_id')!=snap.get('snapshot_id') or pointer.get('generated_at')!=snap.get('generated_at')):
                raise FinalizerError('snapshot/latest binding invalid')
            for ref in snap['data_refs']:
                p=safe_path(root,ref['path']);raw=p.read_bytes()
                if blob(raw)!=ref['blob_sha']: raise FinalizerError('snapshot reference blob mismatch')
                if ref['path'] not in raw_cache:
                    if blob(local_git(repo,'show',f'{confirmed_head}:{PREFIX}{ref["path"]}'))!=ref['blob_sha']:
                        raise FinalizerError('inherited reference identity mismatch')
                raw_cache[ref['path']]=raw
        except (OSError,KeyError,ValueError,TypeError) as exc:
            raise FinalizerError('snapshot closure unavailable') from exc
        if state_outputs:
            try:
                state=json.loads(raw_cache[close_state])
                from datetime import datetime
                stamp=datetime.fromisoformat(state['updated_at'])
                if (nothing_missing or state.get('schema_version')!=1 or state.get('trade_date')!=day
                        or state.get('last_mode')!=collection_mode or stamp.tzinfo is None
                        or state.get('missing')!=sorted(set(str(s) for s in snap['missing']))):
                    raise ValueError('state differs from snapshot/invocation')
            except (ValueError,KeyError,TypeError) as exc:raise FinalizerError('Close missing state invalid') from exc
        raw_cache[snapshot_path]=snap_raw
        raw_cache[latest]=safe_path(root,latest).read_bytes()
        for p in allowed_new-{snapshot_path,latest}: raw_cache[p]=safe_path(root,p).read_bytes()
    from fixed_inventory_offline import build_candidate as build_inventory_candidate, InventoryError
    try:
        frozen=build_inventory_candidate(repo=repo,confirmed_head=confirmed_head,day=day,stage=stage,
            compute_sha=compute_sha,snapshot_path=snapshot_path,
            overlays={p:raw_cache[p] for p in allowed_new-new_captures-state_outputs},
            capture_overlays={p:raw_cache[p] for p in new_captures})
    except InventoryError as exc:
        raise FinalizerError('frozen inventory/proof validation failed: '+str(exc)) from exc
    for p,raw in raw_cache.items():
        if p not in state_outputs and frozen['input_blobs'].get(p)!=blob(raw):raise FinalizerError('frozen output blob drift')
    if nothing_missing:
        if not live or collection_mode not in {'close_retry','close_final'} or eligible_universe is None or claims or new_captures:
            raise FinalizerError('nothing_missing scope mismatch')
        from collect_market_data import _live_close_retry_symbols
        if _live_close_retry_symbols(root,day,list(eligible_universe)):
            raise FinalizerError('nothing_missing contradicts existing Close selection')
    if previous_manifest is not None:
        token=hashlib.sha256(request_id.encode()).hexdigest()[:16]
        prior_stages={'open_30m':{'open_15m'},'open_60m':{'open_15m','open_30m'}}.get(stage,set())
        permitted={f'{PREFIX}collector-state/{day[:7]}/{day}-{prior}-{token}-publication.json':prior for prior in prior_stages}
        if not live or previous_manifest not in permitted:raise FinalizerError('prior publication scope mismatch')
        relative=previous_manifest[len(PREFIX):]
        prior_raw=local_git(repo,'show',f'{confirmed_head}:{previous_manifest}')
        prior=json.loads(prior_raw)
        if prior.get('schema_version')!=1 or prior.get('request_id')!=request_id or prior.get('trade_date')!=day or prior.get('stage')!=permitted[previous_manifest]:
            raise FinalizerError('prior publication identity mismatch')
        refs=prior.get('required_outputs')
        if not isinstance(refs,list) or not refs:raise FinalizerError('prior publication outputs unavailable')
        inherited={relative:prior_raw}
        mutable={inventory,budget[len(PREFIX):]}
        seen=set()
        for ref in refs:
            if not isinstance(ref,dict) or set(ref)!={'path','blob_sha'} or ref['path'] in seen:
                raise FinalizerError('prior publication output malformed')
            path=ref['path'];seen.add(path);safe_path(root,path)
            if path in mutable:
                # Inventory is rebuilt below. A prior paid stage requires the
                # current exact budget directly in this manifest: the verifier
                # does not recursively validate nested publication manifests.
                if path==budget[len(PREFIX):]:
                    inherited[path]=local_git(repo,'show',f'{confirmed_head}:{PREFIX}{path}')
                continue
            raw=local_git(repo,'show',f'{confirmed_head}:{PREFIX}{path}')
            if blob(raw)!=ref['blob_sha']:raise FinalizerError('prior publication output drift')
            inherited[path]=raw
        for path,raw in inherited.items():
            if safe_path(root,path).read_bytes()!=raw or frozen['input_blobs'].get(path)!=blob(raw):
                raise FinalizerError('prior publication frozen drift')
            if path in raw_cache and raw_cache[path]!=raw:raise FinalizerError('prior output collision')
            raw_cache[path]=raw
    inventory_raw=frozen['inventory_bytes']
    if blob(inventory_raw)!=frozen['inventory_blob_sha']:raise FinalizerError('inventory blob drift')
    raw_cache[inventory]=inventory_raw
    token=hashlib.sha256(request_id.encode()).hexdigest()[:16]
    destination=safe_path(root,f'collector-state/{day[:7]}/{day}-{stage}-{token}-publication.json')
    if destination.exists(): raise FinalizerError('candidate manifest already exists')
    manifest=dict(schema_version=1,request_id=request_id,trade_date=day,stage=stage,
                  required_outputs=[dict(path=p,blob_sha=blob(raw)) for p,raw in sorted(raw_cache.items())])
    # Recheck before the two local output writes. No Git commit or push.
    if local_git(repo,'rev-parse','HEAD').decode().strip()!=confirmed_head:
        raise FinalizerError('HEAD changed during validation')
    final_changed=set(filter(None,local_git(repo,'diff','--name-only','-z',confirmed_head).decode().split('\0')))
    final_changed.update(filter(None,local_git(repo,'ls-files','--others','--exclude-standard','-z').decode().split('\0')))
    if final_changed!=changed:raise FinalizerError('output set changed during validation')
    for p,sha in frozen['input_blobs'].items():
        if p in state_outputs:continue  # Updated state is rechecked against captured output bytes below.
        if blob(safe_path(root,p).read_bytes())!=sha:raise FinalizerError('frozen view changed during validation')
    for p,raw in raw_cache.items():
        if p!=inventory and safe_path(root,p).read_bytes()!=raw: raise FinalizerError('output changed during validation')
    inventory_destination=safe_path(root,inventory)
    inventory_destination.parent.mkdir(parents=True,exist_ok=True)
    temporary=None
    try:
        with tempfile.NamedTemporaryFile(dir=inventory_destination.parent,delete=False) as out:
            temporary=Path(out.name);out.write(inventory_raw)
        os.replace(temporary,inventory_destination)
    finally:
        if temporary is not None and temporary.exists():temporary.unlink()
    destination.parent.mkdir(parents=True,exist_ok=True)
    with destination.open('xb') as out: out.write(encode(manifest))
    return destination
