import unittest

from services.ops_tracker import run_stop_key, tracker, workload_stop_key


class OpsTrackerTestCase(unittest.TestCase):
    def setUp(self):
        with tracker._lock:
            tracker._counts.clear()
            tracker._started.clear()

    def test_run_stop_lifecycle(self):
        key = run_stop_key("run_123")
        tracker.begin(key)
        snap = tracker.snapshot()
        self.assertIn("run_123", snap["stopping_run_ids"])
        self.assertTrue(tracker.is_active(key))
        tracker.end(key)
        snap = tracker.snapshot()
        self.assertNotIn("run_123", snap["stopping_run_ids"])
        self.assertFalse(tracker.is_active(key))

    def test_workload_stop_lifecycle(self):
        key = workload_stop_key("workload_x")
        tracker.begin(key)
        self.assertIn("workload_x", tracker.snapshot()["stopping_workload_ids"])
        tracker.end(key)
        self.assertNotIn("workload_x", tracker.snapshot()["stopping_workload_ids"])

    def test_counted_keys_survive_multiple_owners(self):
        tracker.begin("distribute")
        tracker.begin("distribute")
        self.assertTrue(tracker.snapshot()["distributing"])
        tracker.end("distribute")
        self.assertTrue(tracker.snapshot()["distributing"])
        tracker.end("distribute")
        self.assertFalse(tracker.snapshot()["distributing"])

    def test_snapshot_flags(self):
        tracker.begin("launch_run")
        tracker.begin("refresh_quotas")
        tracker.begin("distribute")
        snap = tracker.snapshot()
        self.assertTrue(snap["single_launching"])
        self.assertTrue(snap["refreshing_quotas"])
        self.assertTrue(snap["distributing"])
        self.assertEqual(snap["stopping_run_ids"], [])
        self.assertEqual(snap["stopping_workload_ids"], [])
        self.assertEqual(snap["stopping_kernel_refs"], [])

    def test_shared_key_begin_end_balance(self):
        tracker.end("distribute")
        self.assertFalse(tracker.snapshot()["distributing"])


if __name__ == "__main__":
    unittest.main()
