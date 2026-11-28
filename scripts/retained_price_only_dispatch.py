"""Same retained reconciler's separately authorized, add-only price backup lane."""
from datetime import datetime,timezone
import hashlib,hmac,json,os,re
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from scripts import reconcile_retained_soxx_dispatch as legacy
from scripts import retained_price_only_slots as slots
from scripts import provider_basis_snapshot as basis
from scripts import retained_price_only_source_trust as trust
from scripts import retained_price_only_source_policy as eligibility
from scripts.reviewed_equity_calendar import scheduled_close
from scripts.push_proof_store_commit import verify_exact_output_closure

REQUEST_KIND='daily_price_only_recovery_v2'
DOMAIN=b'RETAINED-DAILY-PRICE-ONLY-DISPATCH-V2\0'
RECEIPT='reconciliation-authority/daily-price-only-receipt.json'
SOURCE_PROOF='reconciliation-authority/qualified-publisher-evidence.json'
SOURCE_SIGNATURE='reconciliation-authority/qualified-publisher-evidence.ed25519'
SOURCE_PREFIX='refs/heads/reconciliation-source-admissions/daily-price-only/'
class ClosedHold(ValueError): pass
def need(ok):
    if not ok:raise ClosedHold('RETAINED_PRICE_ONLY_AUTHORITY_HOLD')

def current():return datetime.now(timezone.utc)

def public_request(env):
    raw=env.get('REQUEST_JSON','').encode('utf-8')
    need(0<len(raw)<=192)
    obj=legacy._strict_json(raw)
    need(type(obj) is dict and set(obj)=={'kind','commitment'} and obj['kind']==REQUEST_KIND)
    # Same exact authenticated hosted surface and actor as the historical
    # operation. Only map the closed discriminator for the shared check.
    return legacy.public_request(env|{'REQUEST_JSON':json.dumps(obj|{'kind':'soxx_web_appendix_recovery_v1'})})

def bound_receipt(raw,sha,commitment,now):
    need(type(raw) is bytes and 0<len(raw)<=128000 and
         hmac.compare_digest(hashlib.sha256(DOMAIN+raw).hexdigest(),commitment))
    x=slots.parse(raw)
    need(set(x)=={'schema_version','kind','action','compute_sha','workflow_ref','store_base','salt',
        'valid_from','valid_until','packages','source_urls','source_approval','legacy_publication','native_gate','expected_outputs'})
    need(type(x['schema_version']) is int and x['schema_version']==1 and
        x['kind']=='retained_daily_price_only_authority' and x['action']=='add_only_prior_session_close' and
        x['compute_sha']==sha and x['workflow_ref']==legacy.WORKFLOW_REF and slots.oid(x['store_base']) and
        type(x['salt']) is str and re.fullmatch(r'[0-9a-f]{64}',x['salt']) and x['salt']!='0'*64)
    window=legacy._receipt_window(x)
    need(window.start<=now<window.end and type(x['packages']) is list and 1<=len(x['packages'])<=8
         and type(x['source_urls']) is dict and len(x['source_urls'])==2)
    for pkg in x['packages']:
        need(type(pkg) is dict and set(pkg)=={'record','observations'})
        row=pkg['record'];need(slots.key(row)==dict(mic='XNMS',symbol='SOXX',asset_class='etf',
            currency='USD',session='regular',basis='split_adjusted_price_only',trade_date=row['trade_date']))
        need(slots.stamp(row['admitted_at'])<=now and type(pkg['observations']) is dict)
    need(type(x['expected_outputs']) is dict and all(
        type(p) is str and slots.oid(v) for p,v in x['expected_outputs'].items()))
    return x,window

def _fetch(root,commit):
    need(slots.oid(commit))
    legacy._git(root,'fetch','--quiet','--no-tags','--no-recurse-submodules','origin',commit)
    need(legacy._git(root,'rev-parse','FETCH_HEAD').decode().strip()==commit)

def materialize_receipt(root,sha,commitment):
    ref=f'refs/heads/reconciliation-receipts/daily-price-only/{sha}/{commitment}'
    legacy._git(root,'init','--quiet')
    legacy._git(root,'remote','add','origin',legacy.STORE_URL)
    oid=legacy._resolve_ref(root,ref);_fetch(root,oid)
    raw=slots.read(root,oid,RECEIPT)
    receipt,window=bound_receipt(raw,sha,commitment,current())
    need(slots.val(root,'rev-list','--parents','-n1',oid).split()==[oid,receipt['store_base']])
    delta=legacy._git(root,'diff-tree','-z','-r','--no-renames','--name-status',receipt['store_base'],oid)
    need(delta==b'A\0'+RECEIPT.encode()+b'\0')
    return ref,oid,receipt,window

def _pin(root,commit,path,digest,blob):
    raw=slots.read(root,commit,path)
    need(raw is not None and len(raw)<=512000 and slots.sha(raw)==digest and slots.blob(raw)==blob)
    return raw

def load_release_trust(compute,sha):
    raw=slots.read(compute,sha,trust.TRUST_PATH)
    need(raw is not None and raw!=trust.EMPTY_POLICY)
    value=slots.parse(raw)
    need(type(value) is dict and type(value.get('keys')) is list and bool(value['keys']))
    return raw

def load_release_eligibility(compute,sha):
    raw=slots.read(compute,sha,eligibility.POLICY_PATH)
    rows=eligibility.load_policy(raw)
    active=[p for p in rows.values() if p['role']=='admission_eligible'
            and slots.stamp(p['valid_from'])<=current()<slots.stamp(p['valid_until'])]
    need(len({p['provider_identity'] for p in active})>=2 and len({p['origin'] for p in active})>=2)
    return rows

def verify_source_admission(root,receipt,trust_policy,compute_sha,approved_sources):
    # This is a separately stageable, independently qualified source authority.
    # Never generate it from the publisher payload or from the dispatch itself.
    pin=receipt['source_approval']
    need(type(pin) is dict and set(pin)=={'ref','commit','sha256','blob_sha','signature_sha256','signature_blob'})
    ref=pin['ref'];commit=pin['commit']
    need(type(ref) is str and re.fullmatch(re.escape(SOURCE_PREFIX)+r'[0-9a-f]{64}',ref) and
         legacy._resolve_ref(root,ref)==commit)
    _fetch(root,commit)
    signed=_pin(root,commit,SOURCE_PROOF,pin['sha256'],pin['blob_sha'])
    signature=_pin(root,commit,SOURCE_SIGNATURE,pin['signature_sha256'],pin['signature_blob'])
    exact_slots=sorted({slots.slot_key(p['record']).rsplit('/',1)[1] for p in receipt['packages']})
    proof=trust.verify_approval(trust_policy,signed,signature,compute=compute_sha,scope=exact_slots,now=current())
    issued=slots.stamp(proof['issued_at'])
    expiry=slots.stamp(proof['expires_at'])
    refs={}
    for package in receipt['packages']:
        for entry in package['record']['evidence']:
            d=package['record']['trade_date'];identity=(d,entry['source_id'])
            need(identity not in refs)
            refs[identity]=(entry,package['observations'][entry['path']].encode(),
                            slots.stamp(package['record']['admitted_at']),package['record'])
    need(type(proof['observations']) is list and len(proof['observations'])==len(refs))
    seen=set();raw_digests=set()
    for obs in proof['observations']:
        fields={'trade_date','source_id','observation_sha256','raw_path','raw_blob','raw_sha256','license_sha256'}
        need(set(obs)==fields)
        identity=(obs['trade_date'],obs['source_id'])
        need(identity in refs and identity not in seen);seen.add(identity)
        expected,observation_bytes,admitted,record=refs[identity]
        profile=approved_sources[identity[1]]
        need(obs['observation_sha256']==expected['sha256']==slots.sha(observation_bytes)
             and obs['license_sha256']==profile['license_sha256'])
        need(re.fullmatch(r'reconciliation-evidence/[a-zA-Z0-9_./-]{1,200}',obs['raw_path']) and
             all(x not in ('','.','..') for x in obs['raw_path'].split('/')))
        raw=_pin(root,commit,obs['raw_path'],obs['raw_sha256'],obs['raw_blob'])
        observed=slots.parse(observation_bytes)
        need(slots.stamp(profile['valid_from'])<=slots.stamp(observed['observed_at'])
             <=issued<=admitted<slots.stamp(profile['valid_until']) and
             observed['kind']=='licensed_regular_daily_price_observation' and
             observed['source_basis']=='split_adjusted_explicit')
        need(eligibility.import_prices(profile,raw,expected=slots.key(record),
                scheduled_close=record['scheduled_close_et'])==observed['ohlc'])
        need((identity[0],obs['raw_sha256']) not in raw_digests)
        raw_digests.add((identity[0],obs['raw_sha256']))
    need(seen==set(refs) and legacy._resolve_ref(root,ref)==commit)
    return ref,commit,expiry

def legacy_occupied(root,x,base):
    need(type(x) is dict and set(x)=={'claim_commit','market_commit','manifest','artifact'})
    old_ref=legacy.claim_ref()
    need(legacy._resolve_ref(root,old_ref)==x['claim_commit'])
    _fetch(root,x['claim_commit'])
    claim=legacy._strict_json(slots.read(root,x['claim_commit'],'claim.json'))
    need(claim['kind']=='soxx_retained_publication_claim' and claim['published_store_commit']==x['market_commit'])
    market=x['market_commit'];manifest=x['manifest'];artifact=x['artifact']
    need(slots.oid(market) and slots.remote(root,slots.TAG+market)==market and
        slots.val(root,'merge-base',market,base)==market)
    need(type(manifest) is str and re.fullmatch(r'data/market-data/collector-state/[a-zA-Z0-9_./-]+\.json',manifest)
         and type(artifact) is str and re.fullmatch(r'data/market-data/recovery/[a-zA-Z0-9_./-]+/manifest\.json',artifact)
         and all(y not in ('','.','..') for p in (manifest,artifact) for y in p.split('/')))
    parent=slots.val(root,'rev-parse',market+'^')
    verify_exact_output_closure(root,parent,market,manifest)
    inventory=legacy._strict_json(slots.read(root,market,manifest))
    need(inventory['stage']=='soxx_public_web_price_appendix' and
         {'path':artifact.removeprefix(slots.ROOT),'blob_sha':slots.blob(slots.read(root,market,artifact))}
         in inventory['required_outputs'])
    original=legacy._strict_json(slots.read(root,market,artifact))
    need(original['kind']=='soxx_public_web_prior_close_appendix' and original['symbol']=='SOXX' and
         original['currency']=='USD' and original['price_session']=='regular' and
         original['basis_semantics']=='public_web_ohlc_post_split_cent_corroborated')
    dates=tuple(row['trade_date'] for row in original['bars']);scope=claim['scope']
    need(len(dates)==10 and dates==tuple(sorted(set(dates))) and scope['trade_dates']==list(dates)
        and scope['symbol']=='SOXX' and scope['action']=='exact_price_appendix_only')
    return {slots.slot_key(dict(mic='XNMS',symbol='SOXX',asset_class='etf',currency='USD',
            session='regular',basis='split_adjusted_price_only',trade_date=d)) for d in dates}

def native_target_state(store,receipt,now):
    gate=receipt['native_gate']
    need(type(gate) is dict and set(gate)=={'selector_sha256','catalog_blob','anchor_date'})
    base=receipt['store_base'];catalog_path=basis.catalog_path(basis.PROVIDER,'SOXX')
    raw=slots.read(store,base,catalog_path)
    need(raw is not None and slots.blob(raw)==gate['catalog_blob'])
    native=basis._parse(raw)
    selector=native|{'read_sha':base}
    authority=basis.ReadAuthority(gate['selector_sha256'],native['admission_blob_sha'])
    def qualified(day):
        return basis.read_price_view(store,selector,authority,fields=['close'],
            required_sessions=[day],knowledge_cutoff=now.isoformat())
    qualified(gate['anchor_date'])  # full retained native provenance/health
    g=basis._store(store)
    snapshot=basis._validate_snapshot(basis._parse(basis._blob(g,base,
        basis.snapshot_path(basis.PROVIDER,'SOXX',selector['basis_id']),
        selector['snapshot_blob_sha'])),selector['basis_id'])
    available={r['datetime'] for r in snapshot['records']}
    requested={p['record']['trade_date'] for p in receipt['packages']}
    if not available.intersection(requested):return 'verified_exact_target_gap'
    need(requested<=available)
    for d in sorted(requested):qualified(d)
    return 'native_already_qualified'


def manifest_request_id(receipt):
    # This is frozen before actual workflow dispatch, so must not depend
    # on the yet-unknown hosted run ID. Bind the latter in the claim instead.
    scope={'base':receipt['store_base'],'salt':receipt['salt'],
           'slots':sorted(slots.slot_key(x['record']) for x in receipt['packages'])}
    return 'retained-price-only-'+slots.sha(slots.encode(scope))[:32]


def run(compute,receipt_store,store,env):
    sha,run_id,commitment=public_request(env)
    token=env.get('MARKET_DATA_STORE_TOKEN','')
    need(type(token) is str and re.fullmatch(r'[A-Za-z0-9_]{1,1024}',token))
    with legacy._environment():
        legacy._source(compute,sha)
        source_eligibility=load_release_eligibility(compute,sha)
        trust_policy=load_release_trust(compute,sha)
    with legacy._environment(token):
        legacy._private_metadata(token)
        ref,receipt_oid,receipt,window=materialize_receipt(receipt_store,sha,commitment)
        approved_sources=eligibility.select(source_eligibility,receipt['source_urls'],now=current())
        source_ref,source_commit,source_expiry=verify_source_admission(receipt_store,receipt,trust_policy,sha,approved_sources)
        base=receipt['store_base']
        legacy._quiet_store_checkout(store,legacy.STORE_URL,SimpleNamespace(head=base))
        legacy._require_origins(compute,store)
        occupied=legacy_occupied(store,receipt['legacy_publication'],base)
        native_state=native_target_state(store,receipt,current())
        packages=tuple((pkg['record'],{p:s.encode() for p,s in pkg['observations'].items()}) for pkg in receipt['packages'])
        calendar={r['trade_date']:scheduled_close(r['trade_date']) for r,_ in packages}
        need(all(calendar[r['trade_date']]==r['scheduled_close_et'] for r,_ in packages))
        def final_check():
            with legacy._environment():legacy._source(compute,sha)
            need(legacy._resolve_ref(receipt_store,source_ref)==source_commit and
                 legacy._resolve_ref(store,legacy.claim_ref())==receipt['legacy_publication']['claim_commit'])
            legacy.require_current_receipt(receipt_store,ref,receipt_oid,window)
            now=current()
            need(now<source_expiry and eligibility.select(source_eligibility,receipt['source_urls'],now=now)==approved_sources)
            return True
        if native_state=='native_already_qualified':
            need(not receipt['expected_outputs'] and final_check() and slots.remote(store,slots.MAIN)==base
                 and slots.remote(store,slots.TAG+base)==base)
            return None
        need(native_state=='verified_exact_target_gap')
        plan=slots.plan(store,base,packages,source_urls=receipt['source_urls'],session_close=calendar,
                        legacy_occupied=occupied,request_id=manifest_request_id(receipt),
                        observation_kind='licensed_regular_daily_price_observation')
        need({p:slots.blob(raw) for p,raw in plan.additions.items()}==receipt['expected_outputs'])
        return slots.commit_atomically(store,plan,before_push=final_check,publication_proof={
            'compute_sha':sha,'run_id':run_id,'receipt_commit':receipt_oid,
            'receipt_commitment':commitment,'source_commit':source_commit})

def main():
    try:
        with legacy._quiet(),TemporaryDirectory(prefix='retained-price-only-private-') as tmp:
            t=Path(tmp);r=t/'receipt';s=t/'store'
            r.mkdir(mode=0o700);s.mkdir(mode=0o700)
            result=run(Path(__file__).resolve().parents[1],r,s,dict(os.environ))
        print('RETAINED_PRICE_ONLY_VERIFIED_NOOP' if result is None else 'RETAINED_PRICE_ONLY_DURABILITY_VERIFIED')
        return 0
    except slots.SamePricesAuthorityChanged:
        print(slots.SAME_PRICES_AUTHORITY_CHANGED)
        return 1
    except Exception:
        print('RETAINED_PRICE_ONLY_STOP_INSPECT_NO_RETRY')
        return 1

if __name__=='__main__':raise SystemExit(main())
