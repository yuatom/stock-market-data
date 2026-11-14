"""Pure retained SOXX initial admission objects; no transport, receipt or writer."""
from scripts import provider_basis_snapshot as B

BASE='d574510647ad8c1cf2f1786e700a39b4e24c2a8d'
BASIS_ID='30e53f9ba6d0b695af87f85687701ac4c861746beed02c05a4cf0b08be6ef27b'
SNAPSHOT_SHA256='1bb03f5ab7285f103cd1c2b611c260069644a8fd8be76b84620ae0464d55b9f8'
ORIGINAL_DIAGNOSTIC_SHA256='f89094771e014be6784584de0a510a52f1dd3bc368f460bb02736d52bb2f1988'
PREFIX='capture-evidence/e3689de50a6a8da8f5449e12cdd51ae64275cac42bc80ab7704b1a570b5398be/SOXX/'
ENVELOPE_BLOB='86bee72830a646ecbff3e83034505e18e2ab169a'
TERMINAL_BLOB='1eb4b3599fae7af7e3ca1f82a9756b1e5b8adf8c'
ENVELOPE_HASH='95709cce803bef143ea5bcad7c3cb2572c3c98721c1b7c38512234ad1bc73b7d'
SOURCE_COMPUTE='072bad6b4ec96673ff26413fbe8afe1d99e203ab'
SOURCE_REQUEST='43252545769-1'
LEGACY='data/market-data/series/daily/twelve_data_basic/SOXX/'
LEGACY_INDEX_BLOB='068fffcd449bfa2732d9be63a97c5d9a5d2d0654'
DIAGNOSTIC_SHA256='11b3101837be5a558eb4c67fca1ba28cdaad8babfabd30a82c033d7f0cf76ed1'
DIAGNOSTIC_PATH='data/market-data/basis-diagnostics/SOXX/'+DIAGNOSTIC_SHA256+'.json'
MANIFEST_PATH='data/market-data/basis-diagnostics/SOXX/'+DIAGNOSTIC_SHA256+'.manifest.json'
SPLIT=dict(kind='forward_split',numerator=3,denominator=1,effective_date='2026-11-05',source_url='https://www.sec.gov/Archives/edgar/data/1100663/000119312526430480/d156277d497.htm')


def reconstruct_snapshot(envelope_raw,baseline_records):
    """Create a new basis using actual recoverable current diagnostic bytes."""
    B._need(B.blob_sha(envelope_raw)==ENVELOPE_BLOB and B.sha(envelope_raw)==ENVELOPE_HASH)
    diagnostic=B.encode(baseline_records)
    snapshot=B.prepare_snapshot(envelope_raw,ENVELOPE_HASH,diagnostic,SPLIT)
    B._need(snapshot['record_count']==318 and snapshot['last_trade_date']=='2026-11-11')
    return B.encode(snapshot),diagnostic


def prepare(snapshot_raw,envelope_raw,terminal_raw,*,admitted_at):
    B._need(B.blob_sha(envelope_raw)==ENVELOPE_BLOB and B.blob_sha(terminal_raw)==TERMINAL_BLOB)
    parsed=B._parse(snapshot_raw);basis_id=parsed['basis_id'];snapshot=B._validate_snapshot(parsed,basis_id)
    envelope=B.verify_envelope(envelope_raw,ENVELOPE_HASH);terminal=B._parse(terminal_raw)
    B._need(snapshot['record_count']==318 and snapshot['last_trade_date']=='2026-11-11' and snapshot['records']==envelope['records'])
    B._need(terminal['status']=='CAPTURE_RETAINED_UNQUALIFIED' and terminal['compute_sha']==SOURCE_COMPUTE and terminal['request_id']==SOURCE_REQUEST
        and terminal['market_qualified'] is False and terminal['production_activation'] is False
        and terminal['envelope_sha256']==snapshot['envelope_sha256'] and terminal['raw_response_sha256']==snapshot['raw_response_sha256'])
    B._need(B._stamp(admitted_at)>=B._stamp(snapshot['observed_at']))
    admission=dict(schema_version=1,kind='provider_snapshot_price_admission',status='ADMITTED_PRICE_ONLY',basis_id=basis_id,
        allowed_fields=list(B.PRICE_FIELDS),volume_policy='BLOCKED_UNRESOLVED',admitted_at=admitted_at,
        proof_model='reviewed_workflow_receipt_not_provider_signature',
        envelope_ref=dict(path=PREFIX+'envelope.json',blob_sha=ENVELOPE_BLOB),
        workflow_evidence=dict(compute_sha=SOURCE_COMPUTE,request_id=SOURCE_REQUEST,terminal_ref=dict(path=PREFIX+'terminal.json',blob_sha=TERMINAL_BLOB)),
        unresolved_volume_dates=snapshot['unresolved_volume_dates'])
    ar=B.encode(admission)
    catalog=dict(schema_version=1,provider=B.PROVIDER,symbol='SOXX',basis_id=basis_id,snapshot_blob_sha=B.blob_sha(snapshot_raw),admission_blob_sha=B.blob_sha(ar))
    updates={B.snapshot_path(B.PROVIDER,'SOXX',basis_id):snapshot_raw,B.admission_path(B.PROVIDER,'SOXX',B.blob_sha(ar)):ar,B.catalog_path(B.PROVIDER,'SOXX'):B.encode(catalog)}
    return dict(basis_id=basis_id,snapshot=snapshot,admission=admission,catalog=catalog,updates=updates)


def prepare_from_store(store,*,admitted_at):
    """Read only fixed immutable Store objects. Caller owns locks and publication."""
    index_raw=store.read(BASE,LEGACY+'_index.json');B._need(B.blob_sha(index_raw)==LEGACY_INDEX_BLOB)
    index=B._parse(index_raw,canonical=False);rows=[];legacy_refs=[dict(path=LEGACY+'_index.json',blob_sha=LEGACY_INDEX_BLOB)]
    for shard in index['shards']:
        B._need(type(shard['path']) is str and '/' not in shard['path'] and shard['path'].endswith('.json'))
        path=LEGACY+shard['path'];raw=store.read(BASE,path);B._need(B.blob_sha(raw)==shard['blob_sha'])
        rows.extend(B._parse(raw,canonical=False)['records']);legacy_refs.append(dict(path=path,blob_sha=shard['blob_sha']))
    B._need(len(rows)==310 and rows[-1]['trade_date']=='2026-10-30')
    envelope=store.read(BASE,PREFIX+'envelope.json');terminal=store.read(BASE,PREFIX+'terminal.json')
    snapshot_raw,diagnostic=reconstruct_snapshot(envelope,rows)
    result=prepare(snapshot_raw,envelope,terminal,admitted_at=admitted_at)
    receipts=B._parse(terminal)['receipts'];B._need(len(receipts)==5)
    for receipt in receipts:
        raw=store.read(receipt['store_commit_sha'],receipt['path'])
        B._need(B.blob_sha(raw)==receipt['blob_sha'] and B.sha(raw)==receipt['sha256'] and raw==store.read(BASE,receipt['path'])
            and receipt['market_qualified'] is False and receipt['production_activation'] is False
            and receipt['durability_ref']=='refs/tags/market-data-read/'+receipt['store_commit_sha'])
    B._need(B.sha(diagnostic)==DIAGNOSTIC_SHA256)
    diagnostic_path=DIAGNOSTIC_PATH
    provenance_path=MANIFEST_PATH
    audit=dict(schema_version=1,kind='new_basis_from_recoverable_diagnostic',
        old_candidate_basis_id=BASIS_ID,old_candidate_snapshot_sha256=SNAPSHOT_SHA256,
        original_diagnostic_sha256=ORIGINAL_DIAGNOSTIC_SHA256,original_diagnostic_raw_recovered=False,
        new_basis_id=result['basis_id'],diagnostic_path=diagnostic_path,diagnostic_sha256=B.sha(diagnostic),diagnostic_blob_sha=B.blob_sha(diagnostic),
        numeric_reconstruction_source_commit=BASE,legacy_refs=legacy_refs,source_receipts=receipts,
        provider_records_unchanged=True,original_candidate_not_modified=True)
    result['updates'][diagnostic_path]=diagnostic
    audit['objects']=[dict(path=path,blob_sha=B.blob_sha(raw),sha256=B.sha(raw)) for path,raw in sorted(result['updates'].items())]
    result['updates'][provenance_path]=B.encode(audit)
    result['reconstruction_audit']=audit
    result['diagnostic_paths']=[diagnostic_path,provenance_path]
    return result
