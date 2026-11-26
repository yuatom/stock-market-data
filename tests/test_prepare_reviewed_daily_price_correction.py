from dataclasses import replace
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts import market_data_store as S
from scripts import retained_verified_price_correction as P
from scripts import prepare_reviewed_daily_price_correction as C
from scripts.verify_store_publication import verify_publication
from push_proof_store_commit import StorePushError, push_exact, verify_exact_output_closure


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), '-c', 'user.name=review fixture',
                                    '-c', 'user.email=fixture@example.invalid',
                                    '-c', 'commit.gpgsign=false', *args]).strip()


def bar(day, **fields):
    return dict(dict(trade_date=day,open=10.0,high=12.0,low=9.0,close=11.0,volume=100), **fields)


class ReviewedCorrection(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.repo = root / 'store'
        self.evidence = root / 'evidence'
        for path in (self.repo, self.evidence):
            path.mkdir()
            git(path, 'init', '-q')
        self.rows = [bar('2026-11-20'), bar('2026-11-23'), bar('2026-11-24')]
        S.append_daily_bars(self.repo / C.ROOT, provider=C.VENDOR, symbol='AAA', asset_class='stocks',
                            bars=self.rows, series_semantics='daily_regular_ohlcv', shard_record_limit=2)
        git(self.repo, 'add', '.'); git(self.repo, 'commit', '-qm', 'base')
        self.base = git(self.repo, 'rev-parse', 'HEAD').decode()
        self.series = C.ROOT + 'series/daily/twelve_data_basic/AAA/'
        self.index = self.series + '_index.json'
        self.active = self.series + '000002.json'
        self.rows = S.read_daily_series(self.repo / C.ROOT, provider=C.VENDOR, symbol="AAA")
        self.retained = [dict(r) for r in self.rows]
        self.retained[-1].update(high=13.0,volume=300.0)
        self.independent = [bar('2026-11-24',high=13.0,volume=250.0)]
        self.review = [dict(trade_date='2026-11-24',
                            expected_old_row_sha256=P.row_sha256(self.rows[-1]),
                            fields={'high':'13.00'})]
        self.scope = C.ApprovedCorrection('unit-tested', self.base,
            git(self.repo, 'rev-parse', self.base + ':' + self.index).decode(),
            git(self.repo, 'rev-parse', self.base + ':' + self.active).decode(),
            'AAA', 'stocks', None, '2026-11-25',
            self.evidence_source('twelve_data', self.retained),
            self.evidence_source('sp_global_market_intelligence', self.independent),
            S.canonical_bytes(self.review))

    def evidence_source(self, provider, records, **fields):
        path = C.ROOT + 'collector-state/2026-11/' + provider.replace('_','-') + '.json'
        p = self.evidence / path
        p.parent.mkdir(parents=True,exist_ok=True)
        value = dict(dict(schema_version=1,source=provider,symbol='AAA',asset_class='stocks',
                     identity_effective_from=None,currency='USD',session='regular',interval='1d',
                     adjustment_semantics='provider_reported',records=records), **fields)
        p.write_bytes(S.canonical_bytes(value) + b'\n')
        git(self.evidence, 'add', path)
        git(self.evidence, 'commit', '-qm', 'retain evidence')
        commit = git(self.evidence, 'rev-parse', 'HEAD').decode()
        return C.Evidence(commit,path,git(self.evidence,'rev-parse',commit + ':' + path).decode(),provider)

    def candidate(self, scope=None):
        return C.prepare_candidate(self.repo, self.evidence, scope or self.scope,
                                   authenticate=lambda s: s == (scope or self.scope))

    def test_real_git_candidate_preserves_preimages_volume_and_base_ref(self):
        candidate, manifest = self.candidate()
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD').decode(),self.base)
        self.assertEqual(git(self.repo, 'status','--porcelain'), b'')
        self.assertEqual(C.verify_candidate(self.repo,self.evidence,self.scope,candidate,
                                           authenticate=lambda s: s==self.scope), manifest)
        before = json.loads(git(self.repo,'show',self.base+':'+self.active))
        after = json.loads(git(self.repo,'show',candidate+':'+self.active))
        self.assertEqual(after['records'],[dict(before['records'][0],high=13.0)])
        self.assertEqual(git(self.repo,'show',candidate+':'+self.series+'000000.json'),
                         git(self.repo,'show',self.base+':'+self.series+'000000.json'))
        paths = git(self.repo,'diff','--name-only',self.base,candidate).decode().splitlines()
        self.assertEqual(len(paths), 6)
        archive = next(p for p in paths if '/recovery/' in p and p.endswith('000002.json'))
        self.assertEqual(git(self.repo,'show',candidate+':'+archive),
                         git(self.repo,'show',self.base+':'+self.active))

    def test_scope_must_be_authenticated(self):
        with self.assertRaisesRegex(C.CorrectionCandidateError,'AUTHORITY_NOT_VERIFIED'):
            C.prepare_candidate(self.repo,self.evidence,self.scope,authenticate=lambda _: False)

    def test_comparison_and_preimage_fail_before_mutation(self):
        bad = self.evidence_source('sp_global_market_intelligence',[bar('2026-11-24',high=12.9)])
        for scope,reason in [(replace(self.scope,comparator=bad),'COMPARATOR_PRICE_MISMATCH'),
                             (replace(self.scope,active_shard_blob_sha='1'*40),'INDEX_COVERAGE_INVALID'),
                             (replace(self.scope,comparator=self.scope.retained),'INDEPENDENT_SOURCE_REQUIRED')]:
            with self.subTest(reason=reason), self.assertRaisesRegex(C.CorrectionCandidateError,reason):
                self.candidate(scope)
        self.assertEqual(git(self.repo,'status','--porcelain'), b'')
        self.assertEqual(git(self.repo,'rev-parse','HEAD').decode(),self.base)

    def test_reviewed_candidate_cannot_silently_use_corporate_basis(self):
        catalog = self.repo / C.ROOT / 'series-active/daily/twelve_data_basic/AAA.json'
        catalog.parent.mkdir(parents=True)
        catalog.write_text('{}')
        git(self.repo,'add','.');git(self.repo,'commit','-qm','catalog selected')
        with self.assertRaisesRegex(C.CorrectionCandidateError,'VERSIONED_BASIS_FORBIDDEN'):
            self.candidate(replace(self.scope,store_base=git(self.repo,'rev-parse','HEAD').decode()))

    def test_unrelated_or_tampered_commit_rejected(self):
        candidate,_ = self.candidate()
        git(self.repo, 'checkout', '--detach', candidate)
        (self.repo / 'outside.json').write_text('{}')
        git(self.repo,'add','.'); git(self.repo,'commit','--amend','--no-edit')
        wrong = git(self.repo,'rev-parse','HEAD').decode()
        with self.assertRaisesRegex(C.CorrectionCandidateError,'CANDIDATE_SCOPE_CHANGED'):
            C.verify_candidate(self.repo,self.evidence,self.scope,wrong,authenticate=lambda _:True)

    def test_local_staging_ref_exact_cas_never_updates_main(self):
        candidate,manifest = self.candidate()
        review_ref = 'refs/reviewed-price-correction/' + self.scope.request_id
        with self.assertRaisesRegex(C.CorrectionCandidateError,'LOCAL_REVIEW_REF_CAS_FAILED'):
            C.install_local_candidate(self.repo,self.evidence,self.scope,candidate,authenticate=lambda s:s==self.scope)
        git(self.repo,'update-ref',review_ref,self.base)
        self.assertEqual(C.install_local_candidate(self.repo,self.evidence,self.scope,candidate,
                                                   authenticate=lambda s:s==self.scope),
                         (review_ref,manifest))
        self.assertEqual(git(self.repo,'rev-parse',review_ref).decode(),candidate)
        self.assertEqual(git(self.repo,'rev-parse','HEAD').decode(),self.base)
        with self.assertRaisesRegex(C.CorrectionCandidateError,'LOCAL_REVIEW_REF_CAS_FAILED'):
            C.install_local_candidate(self.repo,self.evidence,self.scope,candidate,authenticate=lambda s:s==self.scope)

    def test_invalid_comparator_and_unapproved_overlap_are_rejected(self):
        broken = self.evidence_source('sp_global_market_intelligence',[bar('2026-11-24',high=13.0,low=14.0)])
        with self.assertRaisesRegex(C.CorrectionCandidateError,'COMPARATOR_PRICE_INVALID'):
            self.candidate(replace(self.scope,comparator=broken))
        revised = [dict(row) for row in self.retained]
        revised[0]['close'] = 10.5
        pinned = self.evidence_source('twelve_data',revised)
        with self.assertRaisesRegex(P.CorrectionError,'unreviewed price revision'):
            self.candidate(replace(self.scope,retained=pinned))

    def local_transport(self):
        bare = self.repo.parent / 'synthetic-local-origin.git'
        bare.mkdir()
        git(bare, 'init', '--bare', '-q')
        git(self.repo, 'remote', 'add', 'origin', str(bare))
        git(self.repo, 'push', 'origin', self.base + ':refs/heads/main')
        return bare

    def test_shared_publisher_accepts_exact_candidate_and_authentic_store_reader(self):
        bare = self.local_transport()
        candidate,manifest = self.candidate()
        git(self.repo,'checkout','--quiet','--detach',candidate)
        self.assertEqual(C.verify_candidate(self.repo,self.evidence,self.scope,candidate,
                                           authenticate=lambda s: s==self.scope),manifest)
        verify_exact_output_closure(self.repo,self.base,candidate,manifest)
        result = push_exact(self.repo,self.base,candidate,attempts=1)
        self.assertEqual(result['status'],'store_push_verified')
        self.assertEqual(result['push_attempts'],1)
        receipt = verify_publication(self.repo,candidate,required_manifest=manifest)
        self.assertEqual(receipt['status'],'durability_verified')
        self.assertEqual(receipt['verified_changed_paths'],6)
        self.assertEqual(len(receipt['verified_required_outputs']),5)
        self.assertEqual(git(bare,'rev-parse','refs/tags/market-data-read/'+candidate).decode(),candidate)
        self.assertEqual(S.read_daily_series(self.repo/C.ROOT,provider=C.VENDOR,symbol='AAA'),
                         self.rows[:-1]+[dict(self.rows[-1],high=13.0)])

    def test_shared_publisher_does_not_write_after_concurrent_main_advances(self):
        bare = self.local_transport()
        candidate,manifest = self.candidate()
        tree = git(self.repo,'rev-parse',self.base+'^{tree}').decode()
        concurrent = git(self.repo,'commit-tree',tree,'-p',self.base,'-m','external advancement').decode()
        git(self.repo,'push','origin',concurrent+':refs/heads/main')
        git(self.repo,'checkout','--quiet','--detach',candidate)
        verify_exact_output_closure(self.repo,self.base,candidate,manifest)
        with self.assertRaisesRegex(StorePushError,'STORE_REMOTE_MAIN_ADVANCED'):
            push_exact(self.repo,self.base,candidate,attempts=1)
        self.assertEqual(git(bare,'rev-parse','refs/heads/main').decode(),concurrent)
        self.assertEqual(git(bare,'for-each-ref','--format=%(refname)','refs/tags'),b'')
