from pathlib import Path
import json
import os
import re
import subprocess
import sys
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / '.github/workflows/reconcile-retained-daily.yml'

def load():
    return yaml.safe_load(WORKFLOW.read_text())

def test_manual_only_dedicated_route():
    doc=load();events=doc.get('on',doc.get(True))
    assert set(events)=={'workflow_dispatch'}
    assert doc['name']=='Retained Daily Reconciliation'
    assert set(events['workflow_dispatch']['inputs'])=={'expected_compute_sha','request_json'}
    assert all(x['required'] for x in events['workflow_dispatch']['inputs'].values())
    job=doc['jobs']['reconcile']
    assert "github.event_name == 'workflow_dispatch'" in job['if']
    assert "github.ref == 'refs/heads/main'" in job['if']

def test_shared_writer_lock_and_no_provider_surface():
    doc=load();job=doc['jobs']['reconcile'];text=WORKFLOW.read_text()
    assert job['concurrency']=={'group':'market-data-collector','cancel-in-progress':False,'queue':'max'}
    assert job['timeout-minutes']==12
    assert doc['permissions']=={'contents':'read'}
    assert 'TWELVE_DATA_API_KEY' not in text
    assert 'collect_market_data.py' not in text and 'recover_market_data_baseline.py' not in text
    assert 'upload-artifact' not in text
    secrets=re.findall(r'secrets\.([A-Z_]+)',text)
    assert set(secrets)=={'MARKET_DATA_STORE_TOKEN'}

def test_exact_pinned_checkout_and_safe_argv():
    steps=load()['jobs']['reconcile']['steps']
    checks=[s for s in steps if s.get('uses','').startswith('actions/checkout@')]
    assert len(checks)==3
    assert checks[0]['with']['ref']=='${{ inputs.expected_compute_sha }}'
    assert checks[0]['with']['persist-credentials'] is False
    assert checks[1]['with']['ref']=='${{ steps.request.outputs.expected_store_sha }}'
    assert checks[2]['with']['ref']=='${{ steps.request.outputs.source_candidate_sha }}'
    assert checks[2]['with']['persist-credentials'] is False
    assert checks[1]['with']['repository']==checks[2]['with']['repository']=='yuatom/stock-market-data-store'
    publish=steps[-1]
    assert publish['working-directory']=='compute'
    assert '--request-json "$REQUEST_JSON"' in publish['run']
    assert '--compute-sha "$EXPECTED_COMPUTE_SHA"' in publish['run']
    assert '--request-id "$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT"' in publish['run']
    assert '--store-repo ../store' in publish['run'] and '--candidate-repo ../candidate' in publish['run']
    assert '${{' not in publish['run']
    first=steps[0]
    assert first['shell']=='python'
    assert "os.environ['GITHUB_SHA']" in first['run']
    assert "os.environ['GITHUB_REF']" in first['run']

def test_request_validation_precedes_private_checkouts():
    steps=load()['jobs']['reconcile']['steps']
    validation=next(i for i,s in enumerate(steps) if s.get('id')=='request')
    private=[i for i,s in enumerate(steps) if s.get('with',{}).get('repository')=='yuatom/stock-market-data-store']
    assert all(validation<i for i in private)
    assert '--validate-request-only' in steps[validation]['run']
    assert 'cat ' not in steps[validation]['run']

def test_formal_authorities_define_distinct_retained_only_exception():
    plane=yaml.safe_load((ROOT/'config/data-plane.yaml').read_text())['workflow_execution']
    assert plane['retained_daily_reconciliation_entrypoint']=='.github/workflows/reconcile-retained-daily.yml'
    assert plane['retained_daily_reconciliation_request']=='schemas/retained-daily-reconciliation.schema.json'
    assert plane['retained_daily_reconciliation_provider_acquisition_forbidden'] is True
    config=yaml.safe_load((ROOT/'config/market-data-store.yaml').read_text())
    policy=config['series']['baseline_initialization']['retained_candidate_reconciliation']
    assert policy['mode']=='retained_daily_append'
    assert policy['request_authority']=='schemas/retained-daily-reconciliation.schema.json'
    assert policy['provider_requests_allowed'] is False
    assert policy['all_budget_files_byte_immutable'] is True
    assert policy['old_logical_records_and_prior_lineage_immutable'] is True
    assert policy['original_recovery_retry_or_success_claim_forbidden'] is True
    assert policy['overlap_conflicts']=='preserve_existing_and_record_explicitly'
    contract=yaml.safe_load((ROOT/'config/store-publication-durability.yaml').read_text())
    wrapper=contract['writer_scope']['guarded_manual_wrappers']['.github/workflows/reconcile-retained-daily.yml']
    assert wrapper['job']=='reconcile'
    assert wrapper['entrypoint']=='scripts/reconcile_retained_daily.py'
    assert wrapper['publication_helper']=='scripts/verify_store_publication.py'
    assert wrapper['separate_request_specific_output_manifest'] is True

def test_validator_stdout_matches_workflow_identity_parser():
    request=json.loads((ROOT/'schemas/retained-daily-reconciliation.schema.json').read_text())['const']
    command=[sys.executable,str(ROOT/'scripts/reconcile_retained_daily.py'),'--validate-request-only','--request-json',json.dumps(request)]
    result=subprocess.run(command,text=True,capture_output=True,check=False)
    assert result.returncode==0 and result.stderr==''
    identities=json.loads(result.stdout)
    assert identities=={k:request[k] for k in ['expected_store_sha','source_candidate_sha']}
    assert all(re.fullmatch(r'[0-9a-f]{40}',v) for v in identities.values())

def test_workflow_manual_guard_executes_fail_closed(monkeypatch):
    code=load()['jobs']['reconcile']['steps'][0]['run']
    monkeypatch.setenv('EXPECTED_COMPUTE_SHA','a'*40)
    monkeypatch.setenv('GITHUB_SHA','a'*40)
    monkeypatch.setenv('GITHUB_REF','refs/heads/main')
    monkeypatch.setenv('GITHUB_EVENT_NAME','workflow_dispatch')
    exec(compile(code,'workflow_guard','exec'),{})
    for key,bad in [('GITHUB_REF','refs/heads/other'),('GITHUB_EVENT_NAME','schedule'),('GITHUB_SHA','b'*40),('EXPECTED_COMPUTE_SHA','$(false)')]:
        prior=os.environ[key]
        monkeypatch.setenv(key,bad)
        try:
            try:exec(compile(code,'workflow_guard','exec'),{})
            except SystemExit:pass
            else:raise AssertionError('invalid workflow identity accepted')
        finally:monkeypatch.setenv(key,prior)
