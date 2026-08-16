import unittest

from app.services.ops_tracker import tracker, run_stop_key, workload_stop_key


class OpsTrackerTestCase(unittest.TestCase):
    def setUp(self):
        # Fresh tracker per test so tests never leak into each other
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
        # distribute / launch_run / refresh_quotas are shared keys counted per owner
        tracker.begin("distribute")
        tracker.begin("distribute")
        self.assertTrue(tracker.snapshot()["distributing"])
        tracker.end("distribute")  # one owner finishes
        self.assertTrue(tracker.snapshot()["distributing"])  # still another in flight
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
        # end() on a never-begun key must not crash and must stay inactive
        tracker.end("distribute")
        self.assertFalse(tracker.snapshot()["distributing"])


if __name__ == "__main__":
    unittest.main()
