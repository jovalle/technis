from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'files'))
from monitor import failure


class SelfTestResults(unittest.TestCase):
    def test_ata_latest_failure_is_reported(self):
        data = {'ata_smart_self_test_log': {'extended': {'table': [
            {'status': {'value': 112, 'string': 'Completed: read failure'}, 'lifetime_hours': 100},
            {'status': {'value': 0, 'string': 'Completed without error'}, 'lifetime_hours': 90},
        ]}}}
        self.assertIn('read failure', failure(data))

    def test_interrupted_and_successful_tests_are_not_failures(self):
        for status in (0, 16, 32, 240):
            self.assertIsNone(failure({'ata_smart_self_test_log': {'standard': {'table': [
                {'status': {'value': status, 'string': 'status'}, 'lifetime_hours': 100}
            ]}}}))

    def test_nvme_latest_failure_is_reported(self):
        data = {'nvme_self_test_log': {'table': [
            {'self_test_result': {'value': 7, 'string': 'Failed segment'}, 'power_on_hours': 100},
            {'self_test_result': {'value': 0, 'string': 'Completed'}, 'power_on_hours': 90},
        ]}}
        self.assertIn('Failed segment', failure(data))


if __name__ == '__main__':
    unittest.main()
