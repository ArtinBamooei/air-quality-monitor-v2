import unittest
from datetime import datetime, timezone

from src import quality


class DataQualityChecks(unittest.TestCase):
    def test_invalid_value_rejected(self):
        self.assertFalse(quality.check_valid_value(float('nan'))[0])
        self.assertFalse(quality.check_valid_value('12.5')[0])

    def test_impossible_physical_value_rejected(self):
        self.assertFalse(quality.check_physical_range('pm10', -1)[0])
        self.assertTrue(quality.check_physical_range('pm10', 8_000)[0])

    def test_changed_declared_unit_fails_provider(self):
        self.assertFalse(quality.check_units('open_meteo', {'current_units': {'pm10':'mg/m³'}})[0])

    def test_old_observation_marked_stale(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(quality.check_freshness('open_meteo', '2025-12-31T22:00:00+00:00', now)[0], 'stale')

    def test_large_run_failure_share_degrades(self):
        self.assertEqual(quality.check_run_completeness(10, 6), ('degraded', True))

    def test_source_disagreement_retained_and_flagged(self):
        center, spread, flag = quality.check_agreement([10, 100])
        self.assertEqual(center, 55)
        self.assertEqual(spread, 90)
        self.assertEqual(flag, 'low')


if __name__ == '__main__':
    unittest.main()
