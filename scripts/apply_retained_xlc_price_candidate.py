#!/usr/bin/env python3
"""Guarded one-time application of the reviewed owner-scoped XLC candidate."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

import build_retained_xlc_price_candidate as builder
import quarantine_retained_xlc_volume as common
import market_data_store as store
from build_market_data_inventory import build_inventory
from verify_store_publication import verify_publication

BASE=builder.BASE
PREFIX=common.PREFIX
REPORT=PREFIX+'collector-state/2026-11/2026-11-14-XLC-retained-high-correction.json'
MANIFEST=PREFIX+'collector-state/2026-11/2026-11-14-XLC-retained-high-correction-manifest.json'
BUDGET=PREFIX+'collector-state/2026-11/2026-11-14-twelve-data-budget.json'
MODE='retained_xlc_high_correction'
need=common.need
git=common.git


def stage(repo,request_id):
    repo=Path(repo)
    need(git(repo,'rev-parse','HEAD')==BASE and not git(repo,'status','--porcelain'))
    need(not (repo/REPORT).exists() and not (repo/MANIFEST).exists())
    before_budget=(repo/BUDGET).read_bytes()
    with tempfile.TemporaryDirectory(prefix='xlc-owner-candidate-') as td:
        candidate=Path(td)/'candidate'
        audit=builder.build(repo,candidate)
        manifest=json.loads((candidate/'candidate-manifest.json').read_bytes())
        expected={builder.SERIES+'_index.json',builder.SERIES+'000256.json',builder.AUDIT,
                  'recovery/2026-11-14/twelve_data_basic/daily-baseline-rebase/XLC.json'}
        need(set(manifest['changed_paths'])==expected and manifest['base_store_commit']==BASE)
        hashes={x['path']:x['blob_sha'] for x in manifest['files']}
        need(len(hashes)==len(manifest['files']))
        for relative in sorted(expected):
            src=candidate/relative;dst=repo/PREFIX/relative
            need(src.is_file() and store.git_blob_sha_bytes(src.read_bytes())==hashes[relative])
            if relative.startswith(('recovery/','collector-state/')):need(not dst.exists())
            dst.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(src,dst)
        outputs={PREFIX+p for p in expected}
        inventory=PREFIX+'collector-state/market-data-inventory.json'
        store._write_json(repo/inventory,build_inventory(repo/PREFIX));outputs.add(inventory)
        report=dict(schema_version=1,kind=MODE,status='CANDIDATE_VALIDATED',request_id=request_id,trade_date=builder.DATE,
            symbol='XLC',base_store_sha=BASE,source_commit=common.SOURCE,source_path=common.SOURCE_PATH,
            source_blob_sha=common.SOURCE_BLOB,source_sha256=common.SOURCE_HASH,
            old_high=108.9681,new_high=108.97,corrected_date='2026-11-11',appended_dates=['2026-11-12'],
            old_volume_values_preserved=True,volume_policy='BLOCKED_UNRESOLVED',
            rows_are_owner_scoped_correction_not_whole_provider_snapshot=True,
            requests_attempted=0,additional_credits=0,archive_path=audit['archive_path'],
            local_candidate_manifest_blob_sha=store.git_blob_sha_bytes((candidate/'candidate-manifest.json').read_bytes()),
            publication_requires_terminal_receipt=True)
        store._write_json(repo/REPORT,report);outputs.add(REPORT)
        required=[dict(path=p[len(PREFIX):],blob_sha=git(repo,'hash-object','--',str(repo/p))) for p in sorted(outputs)]
        store._write_json(repo/MANIFEST,dict(schema_version=1,request_id=request_id,trade_date=builder.DATE,stage=MODE,required_outputs=required))
        outputs.add(MANIFEST)
        need((repo/BUDGET).read_bytes()==before_budget)
        changed=set(git(repo,'diff','--name-only').splitlines())|set(git(repo,'ls-files','--others','--exclude-standard').splitlines())
        need(changed==outputs)
        return outputs


def execute(repo,compute,compute_sha,request_id):
    need(os.environ.get('GITHUB_ACTIONS')=='true' and os.environ.get('GITHUB_REPOSITORY')=='yuatom/stock-market-data'
         and os.environ.get('GITHUB_REF')=='refs/heads/main' and os.environ.get('GITHUB_SHA')==compute_sha
         and os.environ.get('GITHUB_WORKFLOW')=='Retained XLC High Correction' and os.environ.get('GITHUB_RUN_ATTEMPT')=='1'
         and request_id==os.environ.get('GITHUB_RUN_ID','')+'-1' and re.fullmatch('[0-9a-f]{40}',compute_sha))
    repo=Path(repo).resolve();compute=Path(compute).resolve()
    need(git(compute,'rev-parse','HEAD')==compute_sha)
    for checkout,name in ((repo,'stock-market-data-store'),(compute,'stock-market-data')):
        need(git(checkout,'remote','get-url','origin') in (f'https://github.com/yuatom/{name}',f'https://github.com/yuatom/{name}.git',f'git@github.com:yuatom/{name}.git'))
    need(git(compute,'ls-remote','origin','refs/heads/main').split()==[compute_sha,'refs/heads/main'])
    need(git(repo,'ls-remote','origin','refs/heads/main').split()==[BASE,'refs/heads/main'])
    outputs=stage(repo,request_id)
    git(repo,'add','--',*sorted(outputs))
    git(repo,'-c','user.name=stock-market-data-bot','-c','user.email=stock-market-data-bot@users.noreply.github.com','commit','--no-gpg-sign','-m','data: apply archived XLC owner-scoped high correction and retained tail')
    candidate=git(repo,'rev-parse','HEAD');need(git(repo,'rev-parse','HEAD^')==BASE)
    need(git(repo,'ls-remote','origin','refs/heads/main').split()==[BASE,'refs/heads/main'])
    # Single non-force push; an unknown result stops without retry.
    git(repo,'push','origin',f'{candidate}:refs/heads/main')
    receipt=verify_publication(repo,candidate,required_manifest=MANIFEST)
    return dict(status='OWNER_SCOPED_XLC_CORRECTION_DURABLE',requests_attempted=0,additional_credits=0,volume_policy='BLOCKED_UNRESOLVED',receipt=receipt)


def main():
    p=argparse.ArgumentParser();p.add_argument('--store-repo',required=True);p.add_argument('--compute-sha',required=True);p.add_argument('--request-id',required=True);a=p.parse_args()
    try:result=execute(a.store_repo,Path(__file__).resolve().parents[1],a.compute_sha,a.request_id)
    except Exception:
        print(json.dumps(dict(status='STOPPED_UNCONFIRMED_OR_REJECTED',automatic_retry_allowed=False)));return 1
    print(json.dumps(result));return 0

if __name__=='__main__':raise SystemExit(main())
