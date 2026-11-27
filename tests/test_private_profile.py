import base64
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import yaml
import publish_soxx_web_appendix as W
import soxx_appendix_exact_preflight as P

def fixture():
    end='2035-01-24'
    root=f'data/market-data/recovery/{end}/public-web/soxx-prior-close-v1/'
    receipt='data/market-data/collector-state/2035-01/2035-01-24-soxx_public_web_price_appendix-1122334455667788-publication.json'
    return dict(schema_version=1,kind='soxx_web_appendix_private_profile',salt='a'*64,
                evidence_base='1'*40,base='1'*40,head='2'*40,tree='3'*40,candidate_parents=['1'*40],
                native_path='data/market-data/series-versions/daily/twelve_data_basic/SOXX/'+'4'*64+'/snapshot.json',
                native_blob='5'*40,manifest=receipt,trade_dates=[f'2035-01-{i:02}' for i in range(15,25)],
                outputs={p:{'blob':'6'*40,'sha256':None if p==receipt else 'e'*64}
                         for p in [receipt,*[root+n for n in ('manifest.json','source_sp.json','source_twelve.json')]]},advance=None)

def env(profile=None):
    enc=json.dumps(profile or fixture())
    return dict(GITHUB_REPOSITORY=W.COMPUTE,GITHUB_EVENT_NAME='workflow_dispatch',GITHUB_REF='refs/heads/main',
                GITHUB_ACTOR='yuatom',GITHUB_TRIGGERING_ACTOR='yuatom',GITHUB_RUN_ATTEMPT='1',GITHUB_RUN_ID='321',
                GITHUB_SHA='7'*40,GITHUB_WORKFLOW_SHA='7'*40,GITHUB_WORKFLOW_REF=W.WORKFLOW_REF,
                SOXX_WEB_APPENDIX_PRIVATE_PROFILE=enc,
                SOXX_WEB_APPENDIX_EXACT_RUN_AUTHORITY=json.dumps(W._scope('7'*40,'321',W.profile_commitment(enc.encode()))))

class PrivateProfile(unittest.TestCase):
    def test_independent_approval_commitment_binds_exact_bytes_and_run(self):
        e=env()
        self.assertEqual(W.authorize(e)[2].base,'1'*40)
        for k,v in ((W.PROFILE,json.dumps(dict(fixture(),base='8'*40))),('GITHUB_RUN_ID','322'),
                    ('GITHUB_SHA','8'*40),('GITHUB_RUN_ATTEMPT','2'),
                    (W.AUTHORITY,'{}'),(W.PROFILE,'')):
            with self.subTest(k=k),self.assertRaises(ValueError):
                W.authorize(dict(e,**{k:v}))

    def test_closed_schema_and_narrow_store_paths_after_fresh_commitment(self):
        for bad in (dict(fixture(),unknown=1),dict(fixture(),candidate_parents=['b'*40]),
                    dict(fixture(),native_path='.github/workflows/job.yml'),dict(fixture(),advance={}),
                    dict(fixture(),outputs={**fixture()['outputs'],'.github/workflows/x':{'blob':'9'*40,'sha256':'9'*64}})):
            with self.subTest(keys=bad.keys()),self.assertRaises(ValueError):
                W.authorize(env(bad))

    def test_rejected_binding_never_materializes_private_store(self):
        e=env()
        e[W.PROFILE]='tampered'
        with patch.object(W,'_quiet_store_checkout') as checkout, patch.object(W,'_graphql_one_post') as post:
            with self.assertRaises(ValueError):
                W.publish(Path('/fixture'),Path('/fixture'),e)
        checkout.assert_not_called()
        post.assert_not_called()

    def test_single_cas_with_strict_schema(self):
        plan=W.authorize(env())[2]
        with patch.object(W,'_git', return_value=b'fixture\n'),patch.object(W,'_graphql_one_post',side_effect=OSError) as post:
            with self.assertRaises(ValueError):
                W._create_exact(Path('/fixture'),'token','321',plan)
            post.assert_called_once()
            self.assertEqual(post.call_args.args[1],{
                'query':'mutation ExactAppendix($input: CreateCommitOnBranchInput!) { createCommitOnBranch(input: $input) { clientMutationId commit { oid } } }',
                'variables':{'input':{'branch':{'repositoryNameWithOwner':W.STORE,'branchName':'main'},
                    'expectedHeadOid':'1'*40,'clientMutationId':'soxx-web-appendix-321',
                    'message':{'headline':'data: append retained SOXX public-web prior-close evidence'},
                    'fileChanges':{'additions':[{'path':p,'contents':base64.b64encode(b'fixture\n').decode()} for p in sorted(plan.blobs)]},
                }},
            })

    def test_workflow_has_no_private_checkout_expression_or_public_fields(self):
        repo=Path(__file__).resolve().parents[1]
        wf=yaml.load((repo/'.github/workflows/retained-soxx-web-price-appendix.yml').read_text(),Loader=yaml.BaseLoader)
        self.assertEqual(set(wf['on']),{'workflow_dispatch'})
        self.assertFalse(wf['on']['workflow_dispatch'])
        self.assertEqual(wf['concurrency'],{'group':'market-data-collector','cancel-in-progress':'false','queue':'max'})
        job=wf['jobs']['publish_exact_appendix']
        actions=[step for step in job['steps'] if 'uses' in step]
        (checkout,)=actions
        self.assertEqual(checkout['uses'],'actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5')
        self.assertNotIn('repository',checkout['with'])
        self.assertNotIn('token',checkout['with'])
        self.assertEqual(checkout['with']['ref'],'${{ github.sha }}')
