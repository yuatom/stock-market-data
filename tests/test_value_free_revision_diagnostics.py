"""Fictional rows only; no Git fixture, provider request, or historical replay."""
import copy
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import admit_market_data_baseline as admission
import recover_market_data_baseline as recovery
import replacement_diagnostics as diagnostic


class ValueFreeRevisionTests(unittest.TestCase):
    def setUp(self):
        self.h = admission.load_helpers()
        self.old = [dict(trade_date=f'2020-01-{day:02}', open=10., high=11.,
                         low=9., close=10., volume=1.) for day in range(1, 7)]
        self.new = copy.deepcopy(self.old)
        self.new.append(dict(self.old[-1], trade_date='2020-01-07'))
        self.limits = dict(maximum_revised_overlap_records=3,
                          revised_overlap_must_be_within_last_existing_sessions=3)

    def rejection(self, old=None, new=None):
        with self.assertRaises(self.h.rebaser.ReplacementValidationError) as caught:
            self.h.rebaser.validate_replacement(self.old if old is None else old,
                self.new if new is None else new, **self.limits)
        return caught.exception

    def summary(self):
        evidence = self.rejection().rejection_evidence
        self.assertEqual(evidence['rejection_rule'], 'revision_outside_tail')
        self.assertEqual(evidence['revision_date'], '2020-01-01')
        self.assertIn('revision_difference', evidence)
        return evidence['revision_difference']

    def expected(self, field, kind, old_type, new_type):
        return {'scope': 'first_rejected_row', 'fields': [dict(
            field=field, kind=kind, old_type=old_type, new_type=new_type)],
            'unlisted_field_difference': False}

    def test_value_change_names_field_without_value(self):
        self.new[0]['close'] = 10.56789
        value = self.summary()
        self.assertEqual(value, self.expected('close', 'value_changed', 'float', 'float'))
        self.assertNotIn('10.56789', json.dumps(value))

    def test_equal_integer_float_is_type_change(self):
        self.new[0]['close'] = 10
        self.assertEqual(self.summary(), self.expected('close', 'type_changed', 'float', 'int'))

    def test_same_type_signed_zero_is_representation_change(self):
        self.old[0]['volume'] = 0.
        self.new[0]['volume'] = -0.
        self.assertEqual(self.summary(), self.expected('volume', 'representation_changed', 'float', 'float'))

    def test_bool_integer_and_null_number_keep_exact_type_labels(self):
        for old, new, old_type, new_type in [(True, 1, 'bool', 'int'),
                                            (None, 1., 'null', 'float')]:
            with self.subTest(old_type=old_type):
                self.old[0]['source_timestamp'] = old
                self.new[0]['source_timestamp'] = new
                self.assertEqual(self.summary(), self.expected('source_timestamp', 'type_changed', old_type, new_type))

    def test_nested_values_expose_only_outer_type(self):
        self.old[0]['source_timestamp'] = {'SECRET_CANARY': [1, 'OLD_CANARY']}
        self.new[0]['source_timestamp'] = {'SECRET_CANARY': [2, 'NEW_CANARY']}
        self.assertEqual(self.summary(), self.expected('source_timestamp', 'value_changed', 'dict', 'dict'))

    def test_all_fixed_types_remain_value_free(self):
        for value, label in [(None, 'null'), (False, 'bool'), (1, 'int'), (1., 'float'),
                             ('SECRET_CANARY', 'str'), (['SECRET_CANARY'], 'list'),
                             ({'SECRET_CANARY': 1}, 'dict')]:
            with self.subTest(label=label):
                self.new[0]['source_timestamp'] = value
                self.assertEqual(self.summary(), self.expected('source_timestamp', 'presence_changed', 'missing', label))

    def test_missing_and_null_are_not_conflated(self):
        self.new[0]['source_timestamp'] = None
        self.assertEqual(self.summary(), self.expected('source_timestamp', 'presence_changed', 'missing', 'null'))

    def test_optional_field_removed_does_not_leak_timestamp(self):
        self.old[0]['source_timestamp'] = 'PRIVATE_TIMESTAMP_CANARY'
        value = self.summary()
        self.assertEqual(value, self.expected('source_timestamp', 'presence_changed', 'str', 'missing'))
        self.assertNotIn('PRIVATE_TIMESTAMP_CANARY', json.dumps(value))

    def test_multiple_fields_are_bounded_and_ordered(self):
        for field in ('open', 'high', 'low', 'close', 'volume'):
            self.new[0][field] = 27.
        self.new[0]['source_timestamp'] = 'PRIVATE_TIMESTAMP_CANARY'
        value = self.summary()
        self.assertEqual([x['field'] for x in value['fields']],
                         ['open', 'high', 'low', 'close', 'volume', 'source_timestamp'])
        self.assertEqual(len(value['fields']), 6)
        self.assertFalse(value['unlisted_field_difference'])
        self.assertNotIn('27.0', json.dumps(value))
        self.assertNotIn('PRIVATE_TIMESTAMP_CANARY', json.dumps(value))

    def test_unknown_key_is_only_a_boolean_not_a_name_or_value(self):
        self.old[0]['PRIVATE_FIELD_CANARY'] = {'SECRET_CANARY': 'https://x?apikey=TOKEN_CANARY'}
        self.assertEqual(self.summary(), {'scope': 'first_rejected_row', 'fields': [],
                                         'unlisted_field_difference': True})

    def test_unchanged_unknown_key_is_not_reported(self):
        self.old[0]['PRIVATE_FIELD_CANARY'] = {'SECRET_CANARY': 10}
        self.new[0]['PRIVATE_FIELD_CANARY'] = {'SECRET_CANARY': 10}
        self.new[0]['close'] = 10.5
        self.assertEqual(self.summary(), self.expected('close', 'value_changed', 'float', 'float'))

    def test_first_rejected_row_is_not_a_whole_history_summary(self):
        self.new[0]['close'] = 10.5
        self.new[1]['volume'] = 2.
        self.assertEqual(self.summary(), self.expected('close', 'value_changed', 'float', 'float'))

    def test_inputs_unchanged(self):
        self.new[0]['close'] = 10.5
        before = copy.deepcopy((self.old, self.new))
        self.rejection()
        self.assertEqual((self.old, self.new), before)

    def test_earlier_gates_still_win_without_revision_detail(self):
        self.new[0]['close'] = 10.5
        for rows, rule in [(self.new + [self.new[-1]], 'duplicate_replacement_dates'),
                           (self.new[1:], 'missing_existing_dates')]:
            with self.subTest(rule=rule):
                evidence = self.rejection(new=rows).rejection_evidence
                self.assertEqual(evidence['rejection_rule'], rule)
                self.assertNotIn('revision_difference', evidence)

    def test_unchanged_and_three_tail_revisions_still_pass(self):
        self.assertEqual(self.h.rebaser.validate_replacement(self.old, self.new, **self.limits), [])
        for row in self.new[3:6]:
            row['close'] = 10.5
        revisions = self.h.rebaser.validate_replacement(self.old, self.new, **self.limits)
        self.assertEqual(len(revisions), 3)
        self.assertEqual(revisions[0]['changed_fields'], {'close': {'old': 10., 'new': 10.5}})

    def test_diagnostic_helper_failure_retains_original_rule(self):
        self.new[0]['close'] = 10.5
        with patch.object(diagnostic, 'summarize_revision_difference',
                          side_effect=RuntimeError('SECRET_CANARY'), create=True):
            evidence = self.rejection().rejection_evidence
        self.assertEqual(evidence, {'rejection_rule': 'revision_outside_tail', 'revision_date': '2020-01-01'})

    def test_new_projection_failure_retains_original_rule(self):
        self.new[0]['close'] = 10.5
        with patch.object(diagnostic, 'project_revision_difference',
                          side_effect=RuntimeError('SECRET_CANARY'), create=True):
            evidence = self.rejection().rejection_evidence
        self.assertEqual(evidence, {'rejection_rule': 'revision_outside_tail', 'revision_date': '2020-01-01'})

    def test_per_field_serialization_failure_retains_original_rejection(self):
        self.new[0]['close'] = 10.5
        real = self.h.store.canonical_bytes
        def fail_only_scalar(value):
            if type(value) is float:
                raise ValueError('SECRET_CANARY')
            return real(value)
        with patch.object(self.h.store, 'canonical_bytes', side_effect=fail_only_scalar):
            exc = self.rejection()
        self.assertEqual(exc.rejection_evidence,
                         {'rejection_rule': 'revision_outside_tail', 'revision_date': '2020-01-01'})
        self.assertNotIn('SECRET_CANARY', str(exc))

    def test_projection_failure_through_qualification_keeps_original_code(self):
        self.new[0]['close'] = 10.5
        item = dict(symbol='TEST', existing=self.old, effective=None, target_records=6, outputsize=20)
        with patch.object(diagnostic, 'project_revision_difference', side_effect=RuntimeError('SECRET_CANARY')):
            with self.assertRaises(admission.AdmissionError) as caught:
                recovery.qualify(self.h, item, self.new, '2020-01-08', self.limits)
        self.assertEqual(caught.exception.code, 'replacement_rejected')
        self.assertEqual(caught.exception.evidence['rejection_rule'], 'revision_outside_tail')
        self.assertNotIn('revision_difference', caught.exception.evidence)
        self.assertNotIn('rows', item)

    def test_new_helper_is_not_called_before_or_outside_rejection(self):
        with patch.object(diagnostic, 'summarize_revision_difference', side_effect=RuntimeError('SECRET_CANARY')) as helper:
            self.assertEqual(self.h.rebaser.validate_replacement(self.old, self.new, **self.limits), [])
            self.new[-2]['close'] = 10.5
            self.assertEqual(len(self.h.rebaser.validate_replacement(self.old, self.new, **self.limits)), 1)
            self.assertEqual(self.rejection(new=self.new[1:]).rejection_evidence['rejection_rule'], 'missing_existing_dates')
            self.assertEqual(helper.call_count, 0)

    def test_actual_qualification_and_public_cli_keep_metadata_not_values(self):
        self.new[0]['close'] = 10.56789
        item = dict(symbol='TEST', existing=self.old, effective=None, target_records=6, outputsize=20)
        with self.assertRaises(admission.AdmissionError) as caught:
            recovery.qualify(self.h, item, self.new, '2020-01-08', self.limits)
        exc = caught.exception
        self.assertNotIn('rows', item)
        self.assertNotIn('summary', item)
        # Context attachment is normally performed by recover's qualification
        # boundary. Do not invoke that Git-writing orchestrator in this pure test.
        exc.replacement_diagnostic = diagnostic.ReplacementDiagnostic('TEST', ('TEST',), exc.evidence)
        expected = recovery.failure_summary(exc)
        self.assertEqual(expected['failure_code'], 'replacement_rejected')
        self.assertEqual(expected['replacement_rejection'].get('revision_difference'),
                         self.expected('close', 'value_changed', 'float', 'float'))
        env = dict(GITHUB_ACTIONS='true', GITHUB_EVENT_NAME='workflow_dispatch',
                   GITHUB_REPOSITORY='yuatom/stock-market-data', GITHUB_REF='refs/heads/main',
                   GITHUB_SHA='a'*40, TWELVE_DATA_API_KEY='SECRET_CANARY')
        argv = ['--trade-date', '2020-01-08', '--compute-sha', 'a'*40,
                '--store-sha', 'b'*40, '--store-repo', '/unused', '--request-id', '1-1']
        out = io.StringIO()
        with patch.dict('os.environ', env), patch.object(recovery, 'recover', side_effect=exc), redirect_stdout(out):
            self.assertEqual(recovery.main(argv), 2)
        self.assertEqual(json.loads(out.getvalue()), expected)
        for forbidden in ('10.56789', 'SECRET_CANARY', 'old_canonical_sha256', 'changed_fields'):
            self.assertNotIn(forbidden, out.getvalue())


class ValueFreeProjectionTests(unittest.TestCase):
    def test_projector_keeps_only_valid_bounded_metadata(self):
        change = dict(field='close', kind='value_changed', old_type='float', new_type='float')
        raw = dict(rejection_rule='revision_outside_tail', revision_difference={
            'scope': 'PRIVATE_CANARY', 'fields': [dict(change, old='SECRET_CANARY')] * 100,
            'unlisted_field_difference': True, 'raw': 'SECRET_CANARY'})
        out = diagnostic.project_rejection(raw)
        self.assertEqual(out, {'rejection_rule': 'revision_outside_tail', 'revision_difference': {
            'scope': 'first_rejected_row', 'fields': [change], 'unlisted_field_difference': True}})
        self.assertEqual(diagnostic.project_rejection(out), out)

    def test_projection_does_not_attach_row_detail_to_other_rules(self):
        raw = dict(rejection_rule='missing_existing_dates', revision_difference={
            'fields': [], 'unlisted_field_difference': True})
        self.assertEqual(diagnostic.project_rejection(raw), {'rejection_rule': 'missing_existing_dates'})

    def test_malformed_details_do_not_echo_or_break_projection(self):
        invalid = [None, ['SECRET_CANARY'], 'SECRET_CANARY', {},
                   {'fields': 'SECRET_CANARY', 'unlisted_field_difference': True},
                   {'fields': [], 'unlisted_field_difference': 'SECRET_CANARY'},
                   {'fields': [dict(field='SECRET_CANARY', kind='value_changed', old_type='str', new_type='str')],
                    'unlisted_field_difference': False},
                   {'fields': [dict(field='close', kind=['SECRET_CANARY'], old_type='str', new_type='str')],
                    'unlisted_field_difference': False},
                   {'fields': [dict(field='close', kind='value_changed', old_type={'SECRET_CANARY': 1}, new_type='str')],
                    'unlisted_field_difference': False}]
        for value in invalid:
            with self.subTest(value=value):
                out = diagnostic.project_rejection(dict(rejection_rule='revision_outside_tail', revision_difference=value))
                self.assertEqual(out, {'rejection_rule': 'revision_outside_tail'})


if __name__ == '__main__':
    unittest.main()
