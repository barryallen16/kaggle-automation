"""A kernel stuck in CANCEL_ACKNOWLEDGED still holds a Kaggle session slot.

Regression cover for the wedge that blocked every push on an account showing
zero running kernels: seven kernels were stuck in KernelWorkerStatus.
CANCEL_ACKNOWLEDGED. normalize_kernel_status mapped "cancel*" to "stopped", so
the runs were reaped as finished, the dashboard and the launch pre-flight both
reported the account as free, and every push failed with

    Kernel push error: Maximum batch GPU session count of 2 reached.

The teardown does not clear on its own, and a stop stub cannot release it -
only deleting the kernel frees the slot.
"""

import os
import shutil
import sys
import tempfile
import unittest

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TEST_DIR)
sys.path.insert(0, os.path.join(os.path.dirname(TEST_DIR), "app"))

DATA_TMP = None


def setUpModule():
    global DATA_TMP
    DATA_TMP = tempfile.mkdtemp(prefix="stuck_session_")
    os.environ["AUTOMATION_DATA_DIR"] = DATA_TMP


def tearDownModule():
    if DATA_TMP and os.path.isdir(DATA_TMP):
        shutil.rmtree(DATA_TMP, ignore_errors=True)
    os.environ.pop("AUTOMATION_DATA_DIR", None)


def _fresh():
    import importlib

    import config as cfg
    import database as db

    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(str(cfg.DB_PATH) + suffix)
        except OSError:
            pass
    cfg = importlib.reload(cfg)
    db = importlib.reload(db)
    db.init_db()
    return cfg, db


def _rec(run_id, status, ref="acct/stuck", acc="acct"):
    return {
        "id": run_id,
        "account_username": acc,
        "kernel_slug": ref.split("/", 1)[-1],
        "kernel_ref": ref,
        "code_file": "/tmp/a.py",
        "accelerator": "nvidia-tesla-t4-x2",
        "title": run_id,
        "status": status,
        "enable_internet": 1,
        "is_trial": 0,
        "timeout_seconds": 43200,
        "status_message": "",
        "start_time": "2026-09-29T00:00:00+00:00",
        "kaggle_url": f"https://www.kaggle.com/code/{ref}",
        "workload_id": None,
        "shard_index": None,
        "total_shards": None,
        "log_file": "",
    }


class TestNormalizeKernelStatus(unittest.TestCase):
    def _n(self):
        import importlib

        import services.kaggle_kernel_identity as kki

        return importlib.reload(kki)

    def test_cancel_acknowledged_is_not_stopped(self):
        """The whole bug: this collapsed to 'stopped' and freed the slot."""
        kki = self._n()
        self.assertEqual(
            kki.normalize_kernel_status("KernelWorkerStatus.CANCEL_ACKNOWLEDGED"),
            "cancelling",
        )
        self.assertNotEqual(
            kki.normalize_kernel_status("kernelworkerstatus.cancel_acknowledged"),
            "stopped",
        )

    def test_plain_cancelled_is_still_stopped(self):
        """A fully released cancel is genuinely terminal - don't change that."""
        kki = self._n()
        self.assertEqual(kki.normalize_kernel_status("canceled"), "stopped")
        self.assertEqual(kki.normalize_kernel_status("CANCELLED"), "stopped")

    def test_running_and_queued_unchanged(self):
        kki = self._n()
        self.assertEqual(
            kki.normalize_kernel_status("KernelWorkerStatus.RUNNING"), "running"
        )
        self.assertEqual(
            kki.normalize_kernel_status("KernelWorkerStatus.QUEUED"), "queued"
        )

    def test_cancelling_is_terminal_for_lifecycle(self):
        """Otherwise a stuck kernel pins a `kaggle logs -f` subprocess forever."""
        kki = self._n()
        self.assertIn("cancelling", kki.TERMINAL_KERNEL_STATUSES)
        self.assertIn("stopped", kki.TERMINAL_KERNEL_STATUSES)


class TestSessionHoldingRunsAreQueryable(unittest.TestCase):
    """A finished-but-stuck run must stay visible to slot accounting."""

    def test_cancelling_rows_are_returned_and_active_ones_are_not(self):
        _cfg, db = _fresh()
        db.create_run_record(_rec("run_cancelling", "cancelling"))
        db.create_run_record(_rec("run_running", "running"))
        db.create_run_record(_rec("run_stopped", "stopped"))

        holding = {r["id"] for r in db.get_session_holding_runs()}
        self.assertIn("run_cancelling", holding)
        self.assertNotIn("run_running", holding)
        self.assertNotIn("run_stopped", holding)


class TestCancellingOccupiesASlot(unittest.TestCase):
    """live_busy_sessions must count a cancelling kernel as busy."""

    def test_stuck_session_blocks_the_account(self):
        import asyncio
        import importlib

        _cfg, db = _fresh()
        db.create_run_record(_rec("run_stuck", "cancelling"))

        import services.availability as avail
        import services.kaggle_service as ksmod

        importlib.reload(avail)
        importlib.reload(ksmod)

        async def fake_status(user, ref):
            return {
                "success": True,
                "status": "cancelling",
                "raw": "CANCEL_ACKNOWLEDGED",
            }

        orig = ksmod.KaggleService.get_kernel_status
        ksmod.KaggleService.get_kernel_status = staticmethod(fake_status)
        try:
            busy = asyncio.run(avail.live_busy_sessions(["acct"], True))
            free = avail.free_slots(1, busy.get("acct", 0), True)
        finally:
            ksmod.KaggleService.get_kernel_status = staticmethod(orig)

        self.assertEqual(busy.get("acct"), 1, "a cancelling kernel holds a slot")
        self.assertEqual(free, 1, "one of two slots should remain free")
        self.assertEqual(avail.free_slots(2, busy.get("acct", 0), True), 1)

    def test_finished_kernel_is_reaped_and_frees_the_slot(self):
        import asyncio
        import importlib

        _cfg, db = _fresh()
        db.create_run_record(_rec("run_done", "running", ref="acct/done"))

        import services.availability as avail
        import services.kaggle_service as ksmod

        importlib.reload(avail)
        importlib.reload(ksmod)

        async def fake_status(user, ref):
            return {"success": True, "status": "complete", "raw": "COMPLETE"}

        orig = ksmod.KaggleService.get_kernel_status
        ksmod.KaggleService.get_kernel_status = staticmethod(fake_status)
        try:
            busy = asyncio.run(avail.live_busy_sessions(["acct"], True))
        finally:
            ksmod.KaggleService.get_kernel_status = staticmethod(orig)

        self.assertEqual(busy.get("acct"), 0, "a completed kernel frees its slot")


class TestCapMessageNamesStuckKernels(unittest.TestCase):
    """The rejection must name the stuck kernels, not send you hunting."""

    def test_message_lists_stuck_refs_and_delete_command(self):
        _cfg, db = _fresh()
        db.create_run_record(_rec("run_s1", "cancelling", ref="acct/one-stuck"))
        import importlib

        from routers import runs

        importlib.reload(runs)
        msg = runs._cap_error_message("acct", 1)

        self.assertIn("acct/one-stuck", msg)
        self.assertIn("CANCEL_ACKNOWLEDGED", msg)
        self.assertIn("kaggle kernels delete", msg)

    def test_message_without_stuck_runs_points_at_running_kernels(self):
        _cfg, _db = _fresh()
        import importlib

        from routers import runs

        importlib.reload(runs)
        msg = runs._cap_error_message("acct", 2)
        self.assertIn("no free GPU session", msg)
        self.assertIn("Stop a running kernel", msg)
        self.assertNotIn("kaggle kernels delete", msg)


if __name__ == "__main__":
    unittest.main()
