"""Invented rows only. No provider, repository, historical or publication I/O."""
import copy,json,sys,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import rebase_market_data_baseline as r
import replacement_diagnostics as d
class CountLimitTests(unittest.TestCase):
 def setUp(self):
  self.old=[dict(trade_date=f'2020-01-0{i}',open=12.0,high=1000.0,low=1.0,close=12.0,volume=1.0) for i in range(1,4)]
  self.new=copy.deepcopy(self.old)
 def reject(self,limit=0,tail=1):
  with self.assertRaises(r.ReplacementValidationError) as c:r.validate_replacement(self.old,self.new,maximum_revised_overlap_records=limit,revised_overlap_must_be_within_last_existing_sessions=tail)
  return c.exception.rejection_evidence
 def test_count_limit_first_row_metadata_only(self):
  self.new[-1].update(close=999.125,source_timestamp='SECRET_TIMESTAMP',secret_key='SECRET_CREDENTIAL')
  e=self.reject();self.assertEqual(e['rejection_rule'],'revised_overlap_limit');self.assertEqual(e['revision_date'],'2020-01-03')
  self.assertEqual(e['revised_overlap_records'],1)
  self.assertEqual(e['revision_difference'],dict(scope='first_rejected_row',fields=[dict(field='close',kind='value_changed',old_type='float',new_type='float'),dict(field='source_timestamp',kind='presence_changed',old_type='missing',new_type='str')],unlisted_field_difference=True))
  for secret in ('999.125','SECRET_TIMESTAMP','secret_key','SECRET_CREDENTIAL'):self.assertNotIn(secret,json.dumps(e))
  self.assertEqual(d.project_rejection(e),e)
 def test_sorted_first_revision_and_complete_count(self):
  self.new[0]['close']=12;self.new[2]['volume']=-0.0
  self.old.reverse();self.new.reverse();e=self.reject(limit=1,tail=3)
  self.assertEqual(e['revision_date'],'2020-01-01');self.assertEqual(e['revised_overlap_records'],2)
  self.assertEqual(e['revision_difference']['fields'][0]['kind'],'type_changed')
 def test_original_gate_priority_and_acceptance(self):
  self.new[0]['close']=13.;self.new[-1]['close']=13.
  self.assertEqual(self.reject()['rejection_rule'],'revision_outside_tail')
  accepted=r.validate_replacement(self.old,self.new,maximum_revised_overlap_records=2,revised_overlap_must_be_within_last_existing_sessions=3)
  self.assertEqual(len(accepted),2)
  self.new.pop(0);self.assertEqual(self.reject()['rejection_rule'],'missing_existing_dates')
 def test_diagnostic_failure_preserves_count_rule(self):
  self.new[-1]['close']=13.
  with patch.object(d,'summarize_revision_difference',side_effect=RuntimeError('SECRET')):
   e=self.reject()
  self.assertEqual(e['rejection_rule'],'revised_overlap_limit');self.assertNotIn('revision_difference',e)
 def test_projector_filters_injected_values_and_invalid_date(self):
  e=d.project_rejection(dict(rejection_rule='revised_overlap_limit',revision_date='SECRET',revision_difference=dict(fields=[dict(field='close',kind='value_changed',old_type='float',new_type='float',value='SECRET'),dict(field='SECRET',kind='value_changed',old_type='str',new_type='str')],unlisted_field_difference=False)))
  self.assertNotIn('revision_date',e);self.assertIn('revision_difference',e);self.assertNotIn('SECRET',json.dumps(e))
 def test_unchanged_rows_and_negative_limit_remain_unchanged(self):
  self.assertEqual(r.validate_replacement(self.old,self.new,maximum_revised_overlap_records=0,revised_overlap_must_be_within_last_existing_sessions=1),[])
  self.assertEqual(self.reject(limit=-1)['rejection_rule'],'revised_overlap_limit')
 def test_real_qualification_and_failure_summary_keep_safe_projection(self):
  import admit_market_data_baseline as a
  import recover_market_data_baseline as recovery
  self.new[-1]['close']=999.125
  item=dict(symbol='TEST',existing=self.old,effective=None,target_records=3,outputsize=20)
  with self.assertRaises(a.AdmissionError) as caught:
   recovery.qualify(a.load_helpers(),item,self.new,'2020-01-04',dict(maximum_revised_overlap_records=0,revised_overlap_must_be_within_last_existing_sessions=1))
  exc=caught.exception;self.assertEqual(exc.code,'replacement_rejected')
  exc.replacement_diagnostic=d.ReplacementDiagnostic('TEST',('TEST',),exc.evidence)
  out=recovery.failure_summary(exc)
  self.assertEqual(out['replacement_rejection']['revision_date'],'2020-01-03')
  self.assertEqual(out['replacement_rejection']['revision_difference']['fields'][0]['field'],'close')
  self.assertNotIn('999.125',json.dumps(out));self.assertNotIn('rows',item)
