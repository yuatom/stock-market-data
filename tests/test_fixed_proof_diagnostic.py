import json,tempfile,unittest
from pathlib import Path
import fixed_failure_summary as S
from build_deterministic_metric_proof import MetricProofError,MetricProofUnavailable
class ProofDiagnosticTests(unittest.TestCase):
 def test_real_proof_types_are_bounded_not_unknown(self):
  for cls in (MetricProofError,MetricProofUnavailable):
   with self.subTest(cls=cls):
    value=S.summary(cls('SECRET https://example.invalid/?apikey=SECRET'),'collection')
    self.assertEqual(value,{'status':'fixed_guarded_stopped','exit_code':2,'exception_class':cls.__name__,'failure_stage':'collection'})
    self.assertNotIn('SECRET',json.dumps(value))
 def test_wrong_module_same_name_stays_unknown(self):
  for name in ('MetricProofError','MetricProofUnavailable'):
   cls=type(name,(Exception,),{'__module__':'untrusted_module'})
   self.assertEqual(S.summary(cls('SECRET'),'collection')['exception_class'],'unknown')
 def test_unapproved_proof_class_stays_unknown(self):
  cls=type('UnapprovedProofError',(Exception,),{'__module__':'build_deterministic_metric_proof'})
  self.assertEqual(S.summary(cls('SECRET'),'collection')['exception_class'],'unknown')
 def test_bounded_reader_preserves_only_allowed_fields(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'summary.json';p.write_text(json.dumps({'exception_class':'MetricProofError','failure_stage':'collection','message':'SECRET','exit_code':0}))
   v=S.read_summary(p);self.assertEqual(v['exception_class'],'MetricProofError');self.assertEqual(v['exit_code'],2);self.assertNotIn('SECRET',json.dumps(v))
 def test_no_causal_claim_or_new_fields(self):
  v=S.summary(MetricProofError('SECRET'),'stage_publication')
  self.assertEqual(set(v),{'status','exit_code','exception_class','failure_stage'})
if __name__=='__main__':unittest.main()
