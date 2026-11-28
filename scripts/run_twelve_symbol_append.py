"""Deployment-gated, explicitly pinned capture and conditional append runtime.

LOCAL CANDIDATE ONLY: no deployed workflow or shared-lock proof exists.
No schedule/default collector calls this module. Entrypoint requires official
manual-main identity, private destination metadata and exact approval hash. Raw
capture identity is independent of append qualification. Every unknown write
stops globally without a second request, refund or terminal repair write.
"""
from __future__ import annotations
import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from scripts import eleven_symbol_append as core
from scripts import twelve_symbol_append as twelve
import admit_market_data_baseline as admission
import reconcile_retained_daily as retained
import verify_store_publication as verifier
from build_market_data_inventory import build_inventory
from inclusive_daily_wire import daily_wire_parameters,daily_wire_request_plan
from capture_store_retention import retain_capture_object, _git as safe_git, _EvidenceWriter

REGISTRY = ROOT / 'config/twelve-symbol-qualification.json'
COMPUTE_REPO = 'yuatom/stock-market-data'
STORE_REPO = 'yuatom/stock-market-data-store'
WORKFLOW = 'Twelve Symbol Append'
BASIS_POLICY = 'full_ohlc_overlap_preserve_volume_v1'

class OfficialStop(RuntimeError):
    pass


def need(value):
    if not value:
        raise OfficialStop('TWELVE_APPEND_STOPPED')


def example_request(*, compute, registry_sha, day, store='a'*40, contract_sha='b'*64, cutoff='2024-09-06'):
    return dict(schema_version=1, mode='twelve_symbol_conditional_append', compute_sha=compute,
        expected_store_sha=store, private_contract_sha256=contract_sha, registry_sha256=registry_sha,
        execution_day=day, historical_cutoff=cutoff, request_limit=12, credit_limit=12,
        valid_from=day+'T12:00:00+00:00', valid_until=day+'T23:00:00+00:00',
        evidence_repository=STORE_REPO, evidence_prefix='capture-evidence',
        basis_policy=BASIS_POLICY)


def validate_approval(raw, expected_digest, now):
    try:
        value = core.strict_load(raw)
        need(type(value) is dict and set(value) == set(example_request(compute='',registry_sha='',day='')))
        need(core.sha(core.encode(value)) == expected_digest and re.fullmatch('[0-9a-f]{64}', expected_digest))
        for field, size in [('compute_sha',40),('expected_store_sha',40),('private_contract_sha256',64),('registry_sha256',64)]:
            need(type(value[field]) is str and re.fullmatch('[0-9a-f]{'+str(size)+'}',value[field]))
        expected = example_request(compute=value['compute_sha'],registry_sha=value['registry_sha256'],day=value['execution_day'],store=value['expected_store_sha'],
            contract_sha=value['private_contract_sha256'],cutoff=value['historical_cutoff'])
        for field in set(expected) - {'valid_from','valid_until'}:
            need(core.encode(value[field]) == core.encode(expected[field]))
        need(datetime.fromisoformat(value['historical_cutoff']).date().isoformat()==value['historical_cutoff'])
        start,end = (datetime.fromisoformat(value[k]) for k in ('valid_from','valid_until'))
        need(start.utcoffset() is not None and end.utcoffset() is not None and start <= now < end)
        need(value['execution_day'] == now.astimezone(admission.ET).date().isoformat() and
             value['execution_day'] > value['historical_cutoff'])
        return value
    except Exception:
        raise OfficialStop('TWELVE_APPROVAL_INVALID') from None


def qualify_registry(registry, digest, *, for_append=True):
    """Public identity admits capture only; no operating-MIC alias is invented."""
    need(core.sha(REGISTRY.read_bytes()) == digest)
    need(registry == json.loads(REGISTRY.read_bytes()))
    profiles = registry['profiles']
    need(tuple(x['symbol'] for x in profiles) == twelve.SYMBOLS)
    need(registry['api_docs']['documented_current_defaults'] == {'adjust':'splits','dp':-1})
    result = {}
    for profile in profiles:
        symbol = profile['symbol']
        need(profile.get('capture_identity_verified') is True)
        need(profile['type'] == 'ETF' and profile['url'].startswith('https://twelvedata.com/markets/'))
        need(re.fullmatch('[A-Z][A-Z0-9 ]{0,30}', profile['exchange']) and
             re.fullmatch('[A-Z0-9]{4}', profile['profile_mic_code']))
        if for_append:
            need(profile['response_mic_equivalence_verified'] is True and profile['legacy_basis_equivalence_verified'] is True)
        result[symbol] = dict(provider='twelve_data',symbol=symbol,interval='1day',exchange=profile['exchange'],
            mic_code=profile['profile_mic_code'],currency='USD',type='ETF',adjust='splits',dp=-1,
            order='asc')
    return result


def load_private_contract(repo,approval):
    """Read an immutable private object; no public source pins or ambient inventory."""
    need(not retained.git_text(repo,'for-each-ref','--format=%(refname)','refs/replace/'))
    need('GIT_REPLACE_REF_BASE' not in os.environ)
    graft=Path(retained.git_text(repo,'rev-parse','--git-path','info/grafts'))
    if not graft.is_absolute():graft=Path(repo)/graft
    need(not os.path.lexists(graft))
    head=approval['expected_store_sha'];digest=approval['private_contract_sha256']
    relative=f'capture-contracts/{digest}.json'
    blob=retained.git_text(repo,'rev-parse',head+':'+relative)
    need(retained.git_text(repo,'ls-tree',head,'--',relative)==f'100644 blob {blob}\t{relative}')
    size=retained.git_text(repo,'cat-file','-s',blob)
    need(size.isdigit() and 0<int(size)<=65536)
    raw=retained.git_bytes(repo,'cat-file','blob',blob)
    need(core.sha(raw)==digest and core.store.git_blob_sha_bytes(raw)==blob)
    contract=twelve.validate_contract(raw,digest)
    need(contract['historical_cutoff']==approval['historical_cutoff'])
    baseline=contract['baseline_store_sha']
    need(type(baseline) is str and re.fullmatch('[0-9a-f]{40}',baseline))
    need(retained.git_text(repo,'rev-list','--parents','-n','1',head)==head+' '+baseline)
    need(retained.git_text(repo,'diff-tree','--no-commit-id','--name-status','-r',head)==f'A\t{relative}')
    targets=contract['targets']
    need(type(targets) is list and tuple(x.get('symbol') for x in targets)==twelve.SYMBOLS)
    for target in targets:
        core.validate_scope(target,target['symbol'])
        need(target['addition_dates'][-1]==contract['historical_cutoff'])
        prefix=core.series_prefix(target['symbol'], symbols=twelve.SYMBOLS)
        for name,expected in target['files'].items():
            need(retained.git_text(repo,'ls-tree',baseline,'--',prefix+name)==f'100644 blob {expected}\t{prefix+name}')
    return contract


def private_store_metadata(token):
    """Authenticated read only, never display bearer token or response body."""
    need(type(token) is str and len(token) >= 8)
    req = urllib.request.Request('https://api.github.com/repos/'+STORE_REPO,
        headers={'Authorization':'Bearer '+token,'Accept':'application/vnd.github+json','User-Agent':'stock-market-data'})
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise OfficialStop('PRIVATE_STORE_REDIRECT_REJECTED')
    with urllib.request.build_opener(NoRedirect()).open(req, timeout=20) as response:
        need(response.status == 200 and response.geturl() == 'https://api.github.com/repos/'+STORE_REPO)
        raw = response.read(65537)
    need(len(raw) <= 65536)
    obj = core.strict_load(raw)
    need(obj.get('private') is True and obj.get('full_name') == STORE_REPO)


def execution_guard(compute, repo, compute_sha, request_id, env=None):
    env = os.environ if env is None else env
    expected = dict(GITHUB_ACTIONS='true',GITHUB_EVENT_NAME='workflow_dispatch',GITHUB_REF='refs/heads/main',
        GITHUB_SHA=compute_sha,GITHUB_REPOSITORY=COMPUTE_REPO,GITHUB_WORKFLOW=WORKFLOW,
        GITHUB_WORKFLOW_REF=COMPUTE_REPO+'/.github/workflows/twelve-symbol-append.yml@refs/heads/main')
    need(all(env.get(k) == v for k,v in expected.items()))
    need(request_id == env.get('GITHUB_RUN_ID','')+'-'+env.get('GITHUB_RUN_ATTEMPT','') and
         re.fullmatch('[1-9][0-9]*-[1-9][0-9]*',request_id))
    retained._require_checkout(compute,compute_sha)
    retained._require_origin(compute,COMPUTE_REPO)
    retained._require_origin(repo,STORE_REPO)
    need(retained._remote_main(compute) == compute_sha)


def verify_literal_publication(root, sha, *, required_manifest=None):
    """Keep trusted ephemeral Git auth/safety, but omit raw secret-named inputs."""
    env=dict(os.environ)
    env.pop('TWELVE_DATA_API_KEY',None)
    env.pop('MARKET_DATA_STORE_TOKEN',None)
    # The hosted wrapper's URL-scoped auth and transport restrictions must reach
    # the actual verifier child. Replacing COUNT silently drops them after push.
    raw_count=env.get('GIT_CONFIG_COUNT','0')
    need(re.fullmatch(r'0|[1-9][0-9]*',raw_count) is not None)
    count=int(raw_count);need(count<=64)
    need(all('GIT_CONFIG_KEY_'+str(i) in env and 'GIT_CONFIG_VALUE_'+str(i) in env for i in range(count)))
    for i,(key,value) in enumerate((('core.hooksPath','/dev/null'),('core.fsmonitor','false')),start=count):
        env['GIT_CONFIG_KEY_'+str(i)]=key;env['GIT_CONFIG_VALUE_'+str(i)]=value
    env['GIT_CONFIG_COUNT']=str(count+2)
    command=[sys.executable,str(ROOT/'scripts/verify_store_publication.py'),
             '--store-root',str(root),'--expected-commit',sha,'--writer-status','store_persisted']
    if required_manifest is not None: command+=['--required-manifest',required_manifest]
    result=subprocess.run(command,capture_output=True,env=env,timeout=120,check=False)
    need(result.returncode==0 and len(result.stdout)<=65536)
    return core.strict_load(result.stdout)


class ExactPublisher(_EvidenceWriter):
    """Literal-byte candidate validation before one push, using official verifier."""
    def __init__(self, repo, expected, day, request_id, *, verify=verify_literal_publication):
        super().__init__(repo,expected,day,request_id)
        self.head=expected;self.day=day;self.request_id=request_id;self.verify=verify
        self.required_manifest=None
        self.last_receipt=None
        self.budget_relative=f'data/market-data/collector-state/{day[:7]}/{day}-twelve-data-budget.json'
        self.allowed={self.budget_relative}

    def assert_current(self, expected=None):
        expected=expected or self.head
        need(self.head==expected and safe_git(self.repo,'rev-parse','HEAD')==expected and self.remote_head()==expected)
        need(not safe_git(self.repo,'status','--porcelain') and not safe_git(self.repo,'ls-files','--others'))

    def publish(self, paths, *, expected_report=None):
        need(expected_report is None and paths and set(paths) <= self.allowed)
        self.assert_allowed()
        need(self.remote_head() == self.head and safe_git(self.repo,'rev-parse','HEAD') == self.head)
        parent = self.head
        expected = {}
        for relative in paths:
            path = self.safe_path(relative)
            raw = path.read_bytes()
            blob = safe_git(self.repo,'hash-object','-w','--no-filters','--',str(path))
            need(blob == core.store.git_blob_sha_bytes(raw))
            expected[relative] = blob
            safe_git(self.repo,'update-index','--add','--cacheinfo',f'100644,{blob},{relative}')
        changed = set(safe_git(self.repo,'diff','--cached','--name-only',parent).splitlines())
        need(changed == set(paths))
        tree = safe_git(self.repo,'write-tree')
        candidate = safe_git(self.repo,'-c','user.name=stock-market-data-bot','-c',
            'user.email=stock-market-data-bot@users.noreply.github.com','commit-tree',tree,'-p',parent,
            '-m','data: bounded twelve-symbol append transaction')
        need(safe_git(self.repo,'rev-list','--parents','-n','1',candidate) == candidate+' '+parent)
        need(set(safe_git(self.repo,'diff-tree','--no-commit-id','--name-only','-r',candidate).splitlines()) == set(paths))
        for path,blob in expected.items():
            need(safe_git(self.repo,'ls-tree',candidate,'--',path) == f'100644 blob {blob}\t{path}')
        closure = (verifier._verify_required_manifest(self.repo,candidate,self.required_manifest)
                   if self.required_manifest is not None else None)
        # Source/time/origin guard runs again immediately before any external write.
        self.pre_push_guard()
        need(self.remote_head() == parent)
        safe_git(self.repo,'update-ref','HEAD',candidate,parent)
        safe_git(self.repo,'-c','core.hooksPath=/dev/null','push','origin',candidate+':refs/heads/main')
        receipt = self.verify(self.repo,candidate)
        need(receipt['status'] == 'durability_verified' and receipt['store_commit_sha'] == candidate and
             receipt.get('remote_main_sha') == candidate and
             receipt.get('durability_ref') == 'refs/tags/market-data-read/'+candidate)
        if closure is not None:
            need(receipt.get('verified_required_outputs') == closure)
        need(self.remote_head() == candidate and
             safe_git(self.repo,'ls-remote','origin','refs/tags/market-data-read/'+candidate).split() ==
             [candidate,'refs/tags/market-data-read/'+candidate])
        self.last_receipt=receipt
        self.head = candidate
        return candidate


def market_tree(repo, commit):
    return retained._tree(repo, commit)[0]


def capture_transport(request, key, access, before_request):
    """One bounded raw HTTP call with same-day inclusive cutoff wire semantics."""
    need(type(key) is str and 8 <= len(key) <= 4096 and all(33 <= ord(c) <= 126 for c in key))
    spec = access['twelve_data_basic']
    need(spec['base_url']=='https://api.twelvedata.com' and spec['time_series_path']=='/time_series' and spec['interval_daily']=='1day')
    timeout = access['http']['timeout_seconds']; agent = access['http']['user_agent']
    need(type(timeout) is int and 0 < timeout <= 120 and type(agent) is str and 0 < len(agent) <= 256 and
         all(32 <= ord(c) <= 126 for c in agent))
    params = daily_wire_parameters(request)
    params.update(apikey=key)
    url = 'https://api.twelvedata.com/time_series?'+urllib.parse.urlencode(params)
    before_request()
    return admission.load_helpers().collectors._http_raw(url,timeout=timeout,user_agent=agent,maximum_bytes=core.MAX_RAW_BYTES)


def run_official(*, repo, compute, approval_raw, approval_digest, request_id, key, store_token,
                 clock=admission.utc_now, transport=capture_transport, surface_guard=execution_guard,
                 visibility=private_store_metadata, retain=retain_capture_object):
    """Real orchestration. Injectable read/HTTP guards support isolated Git tests.

CLI supplies only actual default boundaries; it has no test bypass switches.
"""
    attempted = 0
    try:
        approval = validate_approval(approval_raw,approval_digest,clock())
        compute_sha=approval['compute_sha']; day=approval['execution_day']
        repo=Path(repo); compute=Path(compute)
        def fresh():
            now=clock()
            validate_approval(approval_raw,approval_digest,now)
            admission.validate_historical_target(day,approval['historical_cutoff'],maturity,now)
            return now
        def guard():
            surface_guard(compute,repo,compute_sha,request_id)
            fresh()
        requests=qualify_registry(json.loads(REGISTRY.read_bytes()),approval['registry_sha256'],for_append=False)
        surface_guard(compute,repo,compute_sha,request_id); visibility(store_token)
        head=approval['expected_store_sha']
        retained._require_checkout(repo,head); need(retained._remote_main(repo)==head)
        contract=load_private_contract(repo,approval)
        scopes={x['symbol']:x for x in contract['targets']}
        blobs={symbol:scope['files'] for symbol,scope in scopes.items()}
        for symbol,request in requests.items():
            scope=scopes[symbol]
            request.update(start_date=scope['first_date'],end_date=contract['historical_cutoff'],
                           outputsize=scope['old_count']+len(scope['addition_dates']))
        base=market_tree(repo,head)
        for symbol in twelve.SYMBOLS: core.validate_base(base,symbol,blobs[symbol],scopes[symbol], symbols=twelve.SYMBOLS)
        h=admission.load_helpers(); config=h.collectors.load_yaml(compute/'config/market-data-store.yaml')
        access=h.collectors.load_yaml(compute/'config/market-data-collector-access.yaml')
        maturity=config['collector']['twelve_data_daily']['settlement_maturity']['minimum_next_day_et']
        guard()  # No claim/debit before the exact requested cutoff is mature.
        limits=config['collector']['budgets']['twelve_data_basic']
        need(limits['hard_credits_per_day']==200 and limits['hard_credits_per_minute']==8)
        budget_relative=f'data/market-data/collector-state/{day[:7]}/{day}-twelve-data-budget.json'
        admission.validate_budget_file(repo/budget_relative,day,200,8)
        need(admission.read_budget_state(repo/budget_relative,day)['credits']+12<=200)
        need(type(key) is str and type(store_token) is str and min(len(key),len(store_token))>=8)
        forbidden=(key.encode(),store_token.encode())
        deadline=time.monotonic()+min(600,(datetime.fromisoformat(approval['valid_until'])-clock()).total_seconds())
        outcomes=[]
        def retention(symbol,name,payload):
            nonlocal head
            guard()
            prior=head
            market_identity=admission.git(repo,'rev-parse',head+':data/market-data')
            receipt=retain(repo=repo,expected_commit=head,approval_sha256=approval_digest,symbol=symbol,
                name=name,payload=payload,execution_day=day,request_id=request_id,forbidden_bytes=forbidden)
            need(receipt['sha256']==core.sha(payload) and receipt['blob_sha']==core.store.git_blob_sha_bytes(payload))
            need(receipt['path']==f'capture-evidence/{approval_digest}/{symbol}/{name}')
            need(retained.git_bytes(repo,'cat-file','blob',receipt['blob_sha'])==payload)
            need(receipt['durability_ref']=='refs/tags/market-data-read/'+receipt['store_commit_sha'])
            head=receipt['store_commit_sha']
            need(admission.git(repo,'rev-list','--parents','-n','1',head)==head+' '+prior)
            need(admission.git(repo,'rev-parse',head+':data/market-data')==market_identity)
            retained._require_checkout(repo,head); need(retained._remote_main(repo)==head)
            return receipt
        for symbol in twelve.SYMBOLS:
            guard(); core.validate_base(base,symbol,blobs[symbol],scopes[symbol], symbols=twelve.SYMBOLS)
            request=requests[symbol]
            # Stable approval namespace prevents rerunning this permission with a new run ID.
            retention(symbol,'claim.json',core.encode(dict(approval=approval,approval_sha256=approval_digest,request_id=request_id)))
            retention(symbol,'request.json',core.encode(request))
            retention(symbol,'attempt.json',core.encode(dict(attempt=1,compute_sha=compute_sha,execution_day=day,
                wire_request=daily_wire_request_plan(request))))
            writer=ExactPublisher(repo,head,day,request_id)
            writer.pre_push_guard=guard
            debit=admission.debit_before_request(h,writer,repo/'data/market-data',day,limits,
                compute=compute,compute_sha=compute_sha,clock=clock,deadline=deadline)
            head=debit.store_commit_sha
            base[budget_relative]=retained.git_bytes(repo,'cat-file','blob',debit.budget_blob_sha)
            debit_evidence=dict(commit=head,blob=debit.budget_blob_sha,minute=debit.minute,credits=1)
            called=False
            def before_request():
                nonlocal attempted,called
                need(not called and attempted<12)
                guard()
                admission.guard_provider_request(writer,debit,compute,compute_sha,day,clock=clock,deadline=deadline)
                stamp=fresh().astimezone(timezone.utc)  # No slow Git after the shared guard.
                need(stamp.strftime('%Y-%m-%dT%H:%MZ')==debit.minute and stamp.second<58 and time.monotonic()<deadline)
                called=True; attempted+=1
            raw=transport(request,key,access,before_request)
            need(called and type(raw) is bytes and 0<len(raw)<=core.MAX_RAW_BYTES)
            raw_receipt=retention(symbol,'response.raw',raw)
            # No parsing/normalization occurred before confirmed private raw retention.
            # The verified evidence commits preserve the market-data tree identity.
            core.validate_base(base,symbol,blobs[symbol],scopes[symbol], symbols=twelve.SYMBOLS)
            lineage=dict(ingest_kind='twelve_symbol_conditional_append',compute_sha=compute_sha,
                request_id=request_id,approval_sha256=approval_digest,raw_response_sha256=core.sha(raw),
                raw_evidence_commit=raw_receipt['store_commit_sha'],raw_evidence_blob=raw_receipt['blob_sha'],
                debit_commit=debit.store_commit_sha,budget_blob=debit.budget_blob_sha)
            try:
                changes,conflicts=twelve.prepare_candidate(base,symbol,raw,request,lineage,blobs[symbol],scopes[symbol])
            except core.ValidationRejected as error:
                need(str(error) in core.ISOLATABLE)
                outcome=dict(symbol=symbol,status='REJECTED',reason=str(error),new_rows=0)
                publication=None
            else:
                changes,manifest=publication_changes(base,changes,symbol,lineage,conflicts,day,request_id,clock(),scopes[symbol])
                guard()
                writer=ExactPublisher(repo,head,day,request_id,verify=lambda root,sha:
                    verify_literal_publication(root,sha,required_manifest=manifest))
                writer.allowed=set(changes);writer.pre_push_guard=guard;writer.required_manifest=manifest
                for path,content in changes.items():
                    target=writer.safe_path(path);target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(content)
                published=writer.publish(sorted(changes));head=published
                # ExactPublisher proved exact parent and global changed-path/blob closure.
                base.update(changes)
                publication=dict(commit=published,manifest=manifest,durability_ref=writer.last_receipt['durability_ref'],
                    verified_required_outputs=writer.last_receipt['verified_required_outputs'])
                outcome=dict(symbol=symbol,status='APPENDED',new_rows=len(scopes[symbol]['addition_dates']),volume_conflicts=conflicts)
            retention(symbol,'terminal.json',core.encode(dict(outcome=outcome,retention=raw_receipt,
                debit=debit_evidence,publication=publication,request_sha256=core.sha(core.encode(request)))))
            outcomes.append(outcome)
        appended=sum(x['status']=='APPENDED' for x in outcomes)
        status='COMPLETE' if appended==12 else ('PARTIAL' if appended else 'BLOCKED')
        return dict(status=status,requests=attempted,credits=attempted,outcomes=outcomes,store_commit=head,
                    appended_count=appended,rejected_count=12-appended,durable_terminal_receipts_confirmed=True)
    except Exception:
        raise OfficialStop('TWELVE_APPEND_STOPPED_INSPECT_BEFORE_RETRY') from None


def publication_changes(base,changes,symbol,lineage,conflicts,day,request_id,now,scope):
    prefix=core.series_prefix(symbol, symbols=twelve.SYMBOLS)
    stem=f'data/market-data/collector-state/twelve-symbol-append/{request_id}-{symbol}'
    report_path=stem+'.json';manifest=stem+'-outputs.json'
    need(report_path not in base and manifest not in base)
    result=dict(changes)
    result[report_path]=core.encode(dict(schema_version=1,mode='twelve_symbol_conditional_append',
        request_id=request_id,symbol=symbol,trade_date=day,historical_cutoff=scope['addition_dates'][-1],
        new_rows=len(scope['addition_dates']),volume_conflicts=conflicts,lineage=lineage))
    with tempfile.TemporaryDirectory(prefix='eleven-inventory-') as directory:
        stage=Path(directory)
        retained._materialize(stage,dict(base,**result))
        inventory=build_inventory(stage/'data/market-data');inventory['generated_at']=now.isoformat()
        result[core.PREFIX+'collector-state/market-data-inventory.json']=core.encode(inventory)
    closure={prefix+n for n in scope['files']}|{report_path,core.PREFIX+'collector-state/market-data-inventory.json'}
    files=dict(base,**result)
    result[manifest]=core.encode(dict(schema_version=1,request_id=request_id,trade_date=day,
        stage='twelve_symbol_conditional_append',required_outputs=[dict(path=p[len(core.PREFIX):],
            blob_sha=core.store.git_blob_sha_bytes(files[p])) for p in sorted(closure)]))
    return result,manifest


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--approval-json',required=True);parser.add_argument('--approval-sha256',required=True)
    parser.add_argument('--validate-only',action='store_true');parser.add_argument('--store-repo',type=Path)
    parser.add_argument('--request-id')
    args=parser.parse_args(argv)
    try:
        approval=validate_approval(args.approval_json.encode(),args.approval_sha256,admission.utc_now())
        qualify_registry(json.loads(REGISTRY.read_bytes()),approval['registry_sha256'],for_append=False)
        if args.validate_only:
            print(json.dumps({'status':'APPROVAL_VALIDATED'}));return 0
        result=run_official(repo=args.store_repo,compute=ROOT,approval_raw=args.approval_json.encode(),
            approval_digest=args.approval_sha256,request_id=args.request_id,
            key=os.environ.get('TWELVE_DATA_API_KEY',''),store_token=os.environ.get('MARKET_DATA_STORE_TOKEN',''))
        print(json.dumps({'status':result['status'],
            'durable_terminal_receipts_confirmed':result['durable_terminal_receipts_confirmed']},sort_keys=True))
        return 0 if result['status']=='COMPLETE' else 2
    except Exception:
        print(json.dumps({'status':'TWELVE_APPEND_STOPPED_INSPECT_BEFORE_RETRY'}));return 1

if __name__=='__main__':
    raise SystemExit(main())
