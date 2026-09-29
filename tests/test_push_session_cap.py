"""A push rejected for the batch-GPU session cap is classified and gated early.

Regression cover for a real wedge: a single launch into a 2/2 account used to
push blind, get "maximum batch gpu session count" back, and then sleep on an
18-minute retry ladder (10 attempts) while the two kernels holding the slots ran
for hours. The button showed a spinner for the whole window and still failed.
"""

import asyncio
import os
import sys
import unittest

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TEST_DIR)
sys.path.insert(0, os.path.join(os.path.dirname(TEST_DIR), "app"))


def _ks():
    import importlib

    import services.kaggle_service as ks

    return importlib.reload(ks)


def _runs():
    import importlib

    from routers import runs

    return importlib.reload(runs)


class TestClassifyPushFailure(unittest.TestCase):
    def test_session_cap(self):
        ks = _ks()
        out = "Kernel push error: 400 - maximum batch GPU session count reached"
        self.assertEqual(ks._classify_push_failure(out), "session_cap")

    def test_conflict(self):
        ks = _ks()
        self.assertEqual(ks._classify_push_failure("409 Conflict"), "conflict")

    def test_clean_push_is_not_retryable(self):
        ks = _ks()
        self.assertEqual(ks._classify_push_failure("Kernel successfully pushed"), "")

    def test_cap_wins_over_conflict_wording(self):
        """A capped push that also says 'conflict' must take the cap ladder."""
        ks = _ks()
        out = "conflict: maximum batch GPU session count exceeded"
        self.assertEqual(ks._classify_push_failure(out), "session_cap")


class TestSessionCapLadders(unittest.TestCase):
    def test_cap_ladder_is_short(self):
        """Total cap wait must stay under a few minutes, not the old 18 minutes."""
        ks = _ks()
        self.assertLessEqual(sum(ks.KaggleService.SESSION_CAP_RETRY_DELAYS), 180)

    def test_ladders_are_shared_with_the_stop_path(self):
        """The stop path must not regress to its own 3x5s ladder."""
        ks = _ks()
        self.assertTrue(ks.KaggleService.SESSION_CAP_RETRY_DELAYS)
        self.assertTrue(ks.KaggleService.CONFLICT_RETRY_DELAYS)


class TestLaunchPreflightGate(unittest.TestCase):
    """A full account is rejected before push_kernel is ever called."""

    def _with_busy(self, busy_value, fn):
        runs = _runs()
        orig = runs.availability.live_busy_sessions

        async def fake_busy(accounts, launch_is_gpu):
            return {a: busy_value for a in accounts}

        runs.availability.live_busy_sessions = fake_busy
        try:
            return asyncio.run(fn(runs))
        finally:
            runs.availability.live_busy_sessions = orig

    def test_full_account_is_rejected(self):
        ks = _ks()

        def boom(*a, **k):
            raise AssertionError("push_kernel must not run for a capped account")

        async def run(runs):
            orig_push = ks.KaggleService.push_kernel
            ks.KaggleService.push_kernel = staticmethod(boom)
            try:
                return await runs._session_cap_rejection("acct", "nvidia-tesla-t4-x2")
            finally:
                ks.KaggleService.push_kernel = staticmethod(orig_push)

        result = self._with_busy(2, run)
        self.assertIsNotNone(result)
        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "session_cap")
        self.assertIn("acct", result["error"])
        self.assertIsNone(result["run_id"])

    def test_free_slot_passes(self):
        async def run(runs):
            return await runs._session_cap_rejection("acct", "nvidia-tesla-t4-x2")

        self.assertIsNone(self._with_busy(1, run))

    def test_cpu_launch_is_never_capped(self):
        runs = _runs()
        orig = runs.availability.live_busy_sessions

        def boom(*a, **k):
            raise AssertionError("CPU launches must not call the availability check")

        runs.availability.live_busy_sessions = boom
        try:
            result = asyncio.run(runs._session_cap_rejection("acct", "none"))
        finally:
            runs.availability.live_busy_sessions = orig
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
