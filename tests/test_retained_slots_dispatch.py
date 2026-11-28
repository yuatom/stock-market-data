from datetime import datetime,timezone
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
from scripts import retained_price_only_dispatch as D
from scripts import reconcile_retained_soxx_dispatch as L
import json,yaml

class Dispatch(TestCase):
    def env(self):
        pin='a'*40
        return dict(GITHUB_SHA=pin,GITHUB_RUN_ID='42',GITHUB_EVENT_NAME='workflow_dispatch',
            GITHUB_REPOSITORY=L.COMPUTE,GITHUB_REF='refs/heads/main',GITHUB_ACTOR='yuatom',
            GITHUB_ACTOR_ID=L.OWNER_ID,GITHUB_TRIGGERING_ACTOR='yuatom',GITHUB_RUN_ATTEMPT='1',
            GITHUB_WORKFLOW_REF=L.WORKFLOW_REF,GITHUB_WORKFLOW_SHA=pin,EXPECTED_COMPUTE_SHA=pin,
            REQUEST_JSON=json.dumps({'kind':D.REQUEST_KIND,'commitment':'b'*64}))
    def test_source_only_release_cannot_activate_hosted_backup(self):
        root=Path(__file__).resolve().parents[1]
        path='.github/workflows/reconcile-retained-daily.yml'
        current=yaml.safe_load((root/path).read_text())
        triggers=current.get('on',current.get(True))
        self.assertEqual(set(triggers),{'workflow_dispatch'})
        operation=triggers['workflow_dispatch']['inputs']['operation']
        self.assertEqual(operation['options'],['gld','soxx_web_appendix'])
        self.assertEqual(operation['default'],'gld')
        job=current['jobs']['reconcile_soxx']
        self.assertEqual(job['concurrency']['group'],'market-data-collector')
        self.assertTrue(any('scripts.reconcile_retained_soxx_dispatch' in step.get('run','') for step in job['steps']))
        for workflow in (root/'.github/workflows').glob('*'):
            if workflow.is_file():
                text=workflow.read_text()
                self.assertNotIn('retained_price_only_dispatch',text)
                self.assertNotIn('daily_price_only_backup',text)
        contract=yaml.safe_load((root/'config/store-publication-durability.yaml').read_text())
        wrapper=contract['writer_scope']['guarded_manual_wrappers'][path]
        self.assertNotIn('separately_authorized_reusable_prior_close_backup',wrapper)
        self.assertEqual(json.loads((root/D.trust.TRUST_PATH).read_text())['keys'],[])
        sources=D.eligibility.load_policy((root/D.eligibility.POLICY_PATH).read_bytes())
        self.assertTrue(all(item['role']=='corroboration_only' for item in sources.values()))
    def test_owner_event_scope_before_any_private_io(self):
        env=self.env()
        self.assertEqual(D.public_request(env),('a'*40,'42','b'*64))
        for change in (dict(GITHUB_RUN_ATTEMPT='2'),dict(GITHUB_ACTOR_ID='42'),
                       dict(GITHUB_REF='refs/heads/other'),dict(GITHUB_SHA='c'*40),dict(REQUEST_JSON='{}')):
            with self.subTest(change=change),patch.object(L,'_private_metadata') as metadata, \
                 patch.object(D,'materialize_receipt') as receipt, self.assertRaises((ValueError,D.ClosedHold)):
                D.run(None,None,None,env|change)
            metadata.assert_not_called();receipt.assert_not_called()

    def test_missing_independent_admission_never_reaches_git_writer(self):
        from contextlib import nullcontext
        from scripts import retained_price_only_slots as slots
        env=self.env()|{'MARKET_DATA_STORE_TOKEN':'synthetic'}
        with patch.object(L,'_environment',side_effect=lambda *args:nullcontext()), \
             patch.object(L,'_source'),patch.object(D,'load_release_eligibility',return_value={}),patch.object(D,'load_release_trust',return_value=b'mock'),patch.object(L,'_private_metadata'), \
             patch.object(D,'materialize_receipt',return_value=('ref','f'*40,{'source_urls':{}},None)), \
             patch.object(D.eligibility,'select',return_value={}), \
             patch.object(D,'verify_source_admission',side_effect=D.ClosedHold), \
             patch.object(slots,'commit_atomically') as writer, self.assertRaises(D.ClosedHold):
            D.run(None,None,None,env)
        writer.assert_not_called()

    def test_manifest_id_is_preapprovable_and_not_a_github_run_relabel(self):
        r=dict(mic='XNMS',symbol='SOXX',asset_class='etf',currency='USD',session='regular',
               basis='split_adjusted_price_only',trade_date='2026-01-12')
        scope={'store_base':'c'*40,'salt':'d'*64,'packages':[{'record':r}]}
        first=D.manifest_request_id(scope)
        self.assertRegex(first,r'^retained-price-only-[0-9a-f]{32}$')
        self.assertEqual(first,D.manifest_request_id(scope|{'run_id':'999'}))
        self.assertNotEqual(first,D.manifest_request_id(scope|{'store_base':'e'*40}))

    def test_authorized_wrapper_reaches_only_reusable_writer_after_closure(self):
        from contextlib import nullcontext
        from types import SimpleNamespace
        from unittest.mock import Mock
        from scripts import retained_price_only_slots as slots
        env=self.env()|{'MARKET_DATA_STORE_TOKEN':'synthetic'}
        row=dict(mic='XNMS',symbol='SOXX',asset_class='etf',currency='USD',session='regular',
            basis='split_adjusted_price_only',trade_date='2026-01-12',scheduled_close_et='16:00')
        receipt={'store_base':'d'*40,'salt':'e'*64,'legacy_publication':{'claim_commit':'c'*40},
            'packages':[{'record':row,'observations':{}}],'source_urls':{},'expected_outputs':{}}
        accepted=SimpleNamespace(additions={})
        def atomic(*args,**kw):
            self.assertTrue(kw['before_push']())
            self.assertEqual(kw['publication_proof']['run_id'],'42')
            return 'f'*40
        def resolve(root,ref):return 'c'*40 if ref==L.claim_ref() else '5'*40
        with patch.object(L,'_environment',side_effect=lambda *args:nullcontext()), \
             patch.object(L,'_source') as source,patch.object(D,'load_release_eligibility',return_value={}),patch.object(D,'load_release_trust',return_value=b'mock'),patch.object(L,'_private_metadata'), \
             patch.object(L,'_quiet_store_checkout'),patch.object(L,'_require_origins'), \
             patch.object(D,'materialize_receipt',return_value=('ref','4'*40,receipt,None)), \
             patch.object(D.eligibility,'select',return_value={}), \
             patch.object(D,'verify_source_admission',return_value=('source','5'*40,datetime.max.replace(tzinfo=timezone.utc))), \
             patch.object(D,'legacy_occupied',return_value=set()), \
             patch.object(D,'native_target_state',return_value='verified_exact_target_gap'), \
             patch.object(L,'_resolve_ref',side_effect=resolve), \
             patch.object(L,'require_current_receipt') as final_receipt, \
             patch.object(slots,'plan',return_value=accepted) as planned, \
             patch.object(slots,'commit_atomically',side_effect=atomic) as writer:
            self.assertEqual(D.run(None,None,None,env),'f'*40)
        source.assert_called_with(None,'a'*40)
        final_receipt.assert_called_once_with(None,'ref','4'*40,None)
        self.assertRegex(planned.call_args.kwargs['request_id'],r'^retained-price-only-')
        writer.assert_called_once()

    def test_release_without_approved_independent_issuer_stops_before_private_io(self):
        from contextlib import nullcontext
        from scripts import retained_price_only_slots as slots
        from scripts import retained_price_only_source_trust as trust
        env=self.env()|{'MARKET_DATA_STORE_TOKEN':'synthetic'}
        with patch.object(L,'_environment',side_effect=lambda *args:nullcontext()), \
             patch.object(L,'_source'),patch.object(D,'load_release_eligibility',return_value={}), \
             patch.object(slots,'read',return_value=trust.EMPTY_POLICY), \
             patch.object(L,'_private_metadata') as metadata,patch.object(D,'materialize_receipt') as receipt, \
             patch.object(slots,'commit_atomically') as writer,self.assertRaises(D.ClosedHold):
            D.run(None,None,None,env)
        for action in (metadata,receipt,writer):action.assert_not_called()

    def test_no_licensed_profiles_holds_before_key_or_store_even_with_owner(self):
        from contextlib import nullcontext
        from scripts import retained_price_only_slots as slots
        env=self.env()|{'MARKET_DATA_STORE_TOKEN':'synthetic'}
        config=(Path(__file__).resolve().parents[1]/D.eligibility.POLICY_PATH).read_bytes()
        with patch.object(L,'_environment',side_effect=lambda *args:nullcontext()), \
             patch.object(L,'_source'),patch.object(slots,'read',return_value=config), \
             patch.object(D,'load_release_trust') as issuers, patch.object(L,'_private_metadata') as metadata, \
             patch.object(D,'materialize_receipt') as receipt, self.assertRaises(D.ClosedHold):
            D.run(None,None,None,env)
        for action in (issuers,metadata,receipt):action.assert_not_called()
