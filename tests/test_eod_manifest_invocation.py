import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / 'scripts'))
from build_store_publication_manifest import PublicationManifestError, build_manifest
from verify_store_publication import StorePublicationError, _verify_required_manifest

DAY = '2026-11-23'
STAGE = 'previous_session_eod'

class EodManifestByInvocation(unittest.TestCase):
    def setUp(self):
        # No ambient test runner context is used except explicitly injected
        # synthetic step vars, matching the caller-owned GitHub context.
        self.host_keys = {"GITHUB_ACTIONS","GITHUB_REPOSITORY","GITHUB_REPOSITORY_OWNER",
                          "GITHUB_EVENT_NAME","GITHUB_REF","GITHUB_WORKFLOW","GITHUB_WORKFLOW_REF",
                          "GITHUB_RUN_ID","GITHUB_RUN_ATTEMPT","GITHUB_REF_TYPE"}
        reset = patch.dict(os.environ, {k:v for k,v in os.environ.items() if k not in self.host_keys}, clear=True)
        reset.start()
        self.addCleanup(reset.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name).resolve()
        self.git('init', '-q')
        self.store = self.repo / 'data/market-data'
        self.state = self.store / 'collector-state/2026-11'
        self.state.mkdir(parents=True)
        self.result = self.repo / 'result.json'
        self.legacy_id = f'scheduled-{DAY}-{STAGE}'
        self.alias = self.state / f'{DAY}-{STAGE}-{self.token(self.legacy_id)}-publication.json'

    def git(self, *args):
        env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
                   GIT_AUTHOR_NAME='Fixture', GIT_AUTHOR_EMAIL='fixture@example.invalid',
                   GIT_COMMITTER_NAME='Fixture', GIT_COMMITTER_EMAIL='fixture@example.invalid')
        return subprocess.run(['git', '-C', str(self.repo), *args], env=env, check=True,
                              text=True, capture_output=True).stdout.strip()

    @staticmethod
    def token(value):
        return hashlib.sha256(value.encode()).hexdigest()[:16]

    def evidence(self, invocation, event, payload):
        run, attempt = invocation.split('-')
        path = self.state / f'{DAY}-{STAGE}-{self.token(invocation)}-conflicts.json'
        obj = dict(schema_version=1, diagnostic_authority='non_authoritative_conflict_evidence',
                   trade_date=DAY, compute_sha='a'*40, store_input_sha='b'*40,
                   invocation=dict(mode=STAGE, invocation_id=invocation, run_id=run,
                                   run_attempt=attempt, repository='yuatom/stock-market-data',
                                   workflow='Market Data Collector', event=event),
                   conflicts=[], failure_target_count=0, conflict_record_count=0,
                   conflict_diagnostic_record_count=0, conflict_diagnostics_truncated=False)
        path.write_text(json.dumps(obj)+'\n')
        (self.store/'shared-synthetic-output.json').write_text(payload+'\n')
        self.result.write_text(json.dumps(dict(result_state_path=path.relative_to(self.store).as_posix()))+'\n')
        return path

    def arguments(self):
        return dict(store_root=self.store, result_file=self.result, request_id='',
                    trade_date=DAY, stage=STAGE)

    @staticmethod
    def hosted(identity, event, ref="refs/heads/main"):
        run, attempt = identity.split('-')
        return dict(GITHUB_ACTIONS='true', GITHUB_REPOSITORY='yuatom/stock-market-data',
                    GITHUB_REPOSITORY_OWNER='yuatom', GITHUB_EVENT_NAME=event, GITHUB_REF=ref,
                    GITHUB_REF_TYPE='tag' if ref.startswith('refs/tags/') else 'branch',
                    GITHUB_WORKFLOW='Market Data Collector',
                    GITHUB_WORKFLOW_REF='yuatom/stock-market-data/.github/workflows/market-data-collector.yml@'+ref,
                    GITHUB_RUN_ID=run, GITHUB_RUN_ATTEMPT=attempt)

    def build(self, identity='', event=''):
        with patch.dict(os.environ,self.hosted(identity,event)):
            return build_manifest(**self.arguments())

    def save(self):
        self.git('add', 'data')
        self.git('-c', 'commit.gpgsign=false', 'commit', '-qm', 'synthetic manifest output')
        return self.git('rev-parse', 'HEAD')

    def check_at(self, commit, f):
        return _verify_required_manifest(self.repo, commit, f.relative_to(self.repo).as_posix())

    def test_create_only_exact_run_and_legacy_original_snapshot(self):
        one = self.evidence('101-1', 'schedule', 'original run output')
        first = self.build('101-1','schedule')
        p1, a1 = first.read_bytes(), self.alias.read_bytes()
        c1 = self.save()
        self.assertEqual(json.loads(a1)['request_id'], self.legacy_id)
        self.assertEqual(json.loads(p1)['request_id'], 'eod-run-101-1')
        a, b = self.check_at(c1, self.alias), self.check_at(c1, first)
        self.assertTrue(any(x['path'].endswith(one.name) for x in a))
        self.assertTrue(any(x['path'].endswith(one.name) for x in b))
        self.assertTrue(any(x['path'].endswith(self.alias.name) for x in b))

        two = self.evidence('202-1', 'schedule', 'different run output')
        second = self.build('202-1','schedule')
        c2 = self.save()
        self.assertNotEqual(first, second)
        self.assertEqual(first.read_bytes(), p1)
        self.assertEqual(self.alias.read_bytes(), a1)
        self.assertEqual(json.loads(second.read_bytes())['request_id'], 'eod-run-202-1')
        second_outputs = self.check_at(c2, second)
        self.assertTrue(any(x['path'].endswith(two.name) for x in second_outputs))
        self.assertFalse(any(x['path'].endswith(self.alias.name) for x in second_outputs))
        # An old manifest still verifies its exact original commit but is not
        # evidence of changed blobs on a later exact commit.
        self.assertTrue(self.check_at(c1, self.alias))
        with self.assertRaises(StorePublicationError):
            self.check_at(c2, self.alias)
        with self.assertRaises(PublicationManifestError):
            self.build('202-1','schedule')

    def test_manual_valid_but_never_claims_legacy_scheduled_path(self):
        self.evidence('202-1','workflow_dispatch','manual-only')
        unique=self.build('202-1','workflow_dispatch')
        self.assertTrue(unique.exists())
        self.assertFalse(self.alias.exists())

    def test_three_identity_mismatches_leave_no_manifest(self):
        self.evidence('202-1','schedule','untrusted')
        for identity, event in [('303-1','schedule'),('202-2','schedule'),('202-1','workflow_dispatch')]:
            with self.subTest(identity=identity,event=event):
                with self.assertRaises(PublicationManifestError):
                    self.build(identity,event)
        self.assertEqual(list(self.state.glob('*-publication.json')),[])

    def test_queued_workflow_without_new_flags_uses_diagnostic_and_default_context(self):
        self.evidence('101-1','schedule','historical exact alias')
        self.build('101-1','schedule')
        prior = self.alias.read_bytes()
        self.save()
        self.evidence('202-1','schedule','queued legacy definition')
        with patch.dict(os.environ,self.hosted('202-1','schedule')):
            # Actual old Build step passes only these v1 CLI fields.
            cp = subprocess.run([sys.executable, '-B', str(HERE/'scripts/build_store_publication_manifest.py'),
                '--store-root',str(self.store),'--result-file',str(self.result),'--request-id','',
                '--trade-date',DAY,'--stage',STAGE], capture_output=True,text=True,check=True)
        two=self.store/cp.stdout.strip()
        self.assertTrue(two.is_file())
        self.assertEqual(json.loads(two.read_bytes())['request_id'],'eod-run-202-1')
        self.assertEqual(prior,self.alias.read_bytes())
        self.assertTrue(self.check_at(self.save(),two))

    def test_github_context_absent_incomplete_or_conflicting_cannot_claim_scope(self):
        self.evidence('202-1','schedule','test scope')
        # No GitHub caller context is local-only, never formal run scope.
        local = build_manifest(**self.arguments())
        self.assertEqual(local, self.alias)
        self.assertFalse(list(self.state.glob('*-run-*-publication.json')))
        self.alias.unlink()  # scratch fixture; malformed hosted cases write nothing
        env=self.hosted('202-1','schedule')
        for key,value in [('GITHUB_ACTIONS',''),('GITHUB_REPOSITORY','other/repo'),
                         ('GITHUB_RUN_ID',''),('GITHUB_WORKFLOW_REF','other'),('GITHUB_REF','refs/heads/dev'),
                         ('GITHUB_RUN_ATTEMPT','2'),('GITHUB_EVENT_NAME','workflow_dispatch')]:
            with self.subTest(key=key), patch.dict(os.environ,dict(env,**{key:value})):
                with self.assertRaises(PublicationManifestError):
                    build_manifest(**self.arguments())
        self.assertFalse(list(self.state.glob('*-publication.json')))

    def test_manual_same_repo_supported_branch_or_tag_keeps_native_admission(self):
        for n, ref in enumerate(("refs/heads/main", "refs/heads/fix/test+中文", "refs/tags/v1.2.3")):
            run=f"{100+n}-1"
            with self.subTest(ref=ref):
                self.evidence(run,"workflow_dispatch","independent target")
                with patch.dict(os.environ,self.hosted(run,"workflow_dispatch",ref)):
                    got=build_manifest(**self.arguments())
                self.assertEqual(json.loads(got.read_bytes())["request_id"],f"eod-run-{run}")
                self.assertTrue(self.check_at(self.save(),got))
                self.assertFalse(self.alias.exists())

    def test_ref_mismatch_foreign_workflow_and_nonmain_schedule_fail_closed(self):
        flow="yuatom/stock-market-data/.github/workflows/market-data-collector.yml@"
        ref="refs/heads/fix/feature"
        self.evidence("777-1","workflow_dispatch","not authorized")
        base=self.hosted("777-1","workflow_dispatch",ref)
        for change in ({"GITHUB_WORKFLOW_REF":flow+"refs/heads/main"},
                       {"GITHUB_WORKFLOW_REF":flow.replace("collector.yml","other.yml")+ref},
                       {"GITHUB_WORKFLOW_REF":flow.replace("yuatom/","other/")+ref},
                       {"GITHUB_REF_TYPE":"tag"},
                       {"GITHUB_REF":"refs/pull/12/merge","GITHUB_WORKFLOW_REF":flow+"refs/pull/12/merge"},
                       {"GITHUB_REF":"refs/heads/invalid..ref","GITHUB_WORKFLOW_REF":flow+"refs/heads/invalid..ref"}):
            with self.subTest(change=change), patch.dict(os.environ,dict(base,**change)):
                with self.assertRaises(PublicationManifestError):
                    build_manifest(**self.arguments())
        self.evidence("888-1","schedule","still not authorized")
        with patch.dict(os.environ,self.hosted("888-1","schedule",ref)):
            with self.assertRaises(PublicationManifestError):
                build_manifest(**self.arguments())
        self.assertFalse(list(self.state.glob("*-publication.json")))

    def test_cli_keeps_original_flag_surface(self):
        script = HERE/'scripts/build_store_publication_manifest.py'
        helptext = subprocess.run([sys.executable, '-B', str(script), '--help'], check=True,
                                  capture_output=True, text=True).stdout
        self.assertNotIn('--eod-invocation-id',helptext)
        self.assertNotIn('--eod-trigger',helptext)
        self.assertIn('--request-id',helptext)

    def test_legacy_python_caller_unchanged(self):
        self.evidence('101-1', 'schedule', 'old-compatible')
        got = build_manifest(store_root=self.store,result_file=self.result,request_id='',
                             trade_date=DAY,stage=STAGE)
        self.assertEqual(got,self.alias)
        self.assertTrue(self.check_at(self.save(),self.alias))
        old = self.alias.read_bytes()
        self.evidence('202-1','schedule','must-not-overwrite-legacy')
        with self.assertRaises(PublicationManifestError):
            build_manifest(store_root=self.store,result_file=self.result,request_id='',trade_date=DAY,stage=STAGE)
        self.assertEqual(old,self.alias.read_bytes())

if __name__ == '__main__':
    unittest.main()
