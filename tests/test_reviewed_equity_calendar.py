import unittest
from unittest.mock import patch
from reviewed_equity_calendar import missing_repair_sessions, scheduled_close
import reviewed_equity_calendar as calendar


class ReviewedEquityCalendar(unittest.TestCase):
    def test_thanksgiving_uses_last_actual_session(self):
        sessions = missing_repair_sessions('2026-11-27')
        self.assertIn('2026-11-25', sessions)
        self.assertNotIn('2026-11-26', sessions)
        self.assertNotIn('2026-11-27', sessions)
        self.assertEqual(scheduled_close('2026-11-27'), '13:00')
        self.assertIsNone(scheduled_close('2026-11-26'))

    def test_cross_year_without_approved_current_calendar_cannot_fill_dates(self):
        self.assertEqual(missing_repair_sessions('2027-01-04'), frozenset())

    def test_weekends_and_holidays_are_not_admitted(self):
        scope = missing_repair_sessions('2026-07-07')
        self.assertNotIn('2026-07-03', scope)
        self.assertNotIn('2026-07-04', scope)
        self.assertIn('2026-07-06', scope)

    def test_malformed_authority_does_not_admit_missing_dates(self):
        original = calendar._load
        with patch.object(calendar, '_load', side_effect=ValueError('bad data')):
            self.assertEqual(missing_repair_sessions('2026-11-27'), frozenset())
        self.assertIsNotNone(original('2026-11-27'))
