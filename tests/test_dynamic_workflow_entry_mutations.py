"""Reachable YAML mutations of the actual writer-boundary assertion, no network."""
import copy
import io
import unittest
from unittest.mock import patch
import test_data_plane_write_boundary as boundary

class DynamicEntryMutationTests(unittest.TestCase):
    def assert_rejected(self, mutate):
        original = boundary.yaml.safe_load
        touched = []
        def load(text):
            doc = original(text)
            if isinstance(doc, dict) and doc.get('name') == 'Dynamic Candidate Collector Runtime':
                doc = copy.deepcopy(doc)
                step = next(s for s in doc['jobs']['collect']['steps'] if s.get('id') == 'collect')
                mutate(step)
                touched.append(True)
            return doc
        case = boundary.DataPlaneWriteBoundaryTest('test_every_direct_store_writer_uses_one_serialization_lane_and_durability_helper')
        output = io.StringIO()
        with patch.object(boundary.yaml, 'safe_load', side_effect=load):
            result = unittest.TextTestRunner(stream=output).run(unittest.TestSuite([case]))
        self.assertEqual(touched, [True])
        self.assertFalse(result.errors, output.getvalue())
        self.assertEqual(len(result.failures), 1, output.getvalue())

    def test_comment_only_cannot_stand_in_for_execution(self):
        self.assert_rejected(lambda s: s.update(run='# python -B scripts/run_dynamic_guarded.py\ntrue\n'))

    def test_echo_only_cannot_stand_in_for_execution(self):
        self.assert_rejected(lambda s: s.update(run='echo "python -B scripts/run_dynamic_guarded.py"\n'))

    def test_exit_before_entry_cannot_stand_in_for_execution(self):
        self.assert_rejected(lambda s: s.update(run='exit 0\n'+s['run']))

    def test_false_step_condition_cannot_stand_in_for_execution(self):
        self.assert_rejected(lambda s: s.update({'if': '${{ false }}'}))

    def test_swallowed_failure_is_rejected(self):
        self.assert_rejected(lambda s: s.update({'continue-on-error': True}))

    def test_dead_shell_branch_is_rejected(self):
        self.assert_rejected(lambda s: s.update(run='if false; then\n'+s['run']+'\nfi\n'))

    def test_repeated_entry_is_rejected(self):
        self.assert_rejected(lambda s: s.update(run=s['run']+'\n'+s['run']))
