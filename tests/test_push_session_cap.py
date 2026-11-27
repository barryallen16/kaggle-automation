import asyncio
import importlib
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
    DATA_TMP = tempfile.mkdtemp(prefix="push_session_cap_")
    os.environ["AUTOMATION_DATA_DIR"] = DATA_TMP


def tearDownModule():
    if DATA_TMP and os.path.isdir(DATA_TMP):
        shutil.rmtree(DATA_TMP, ignore_errors=True)
    os.environ.pop("AUTOMATION_DATA_DIR", None)


def _ks():
    return importlib.reload(__import__("services.kaggle_service", fromlist=["x"]))


def _runs():
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
        ks = _ks()
        out = "conflict: maximum batch GPU session count exceeded"
        self.assertEqual(ks._classify_push_failure(out), "session_cap")


class TestSessionCapLadders(unittest.TestCase):
    def test_cap_ladder_is_one_short_retry(self):
        ks = _ks()
        delays = ks.KaggleService.SESSION_CAP_RETRY_DELAYS
        self.assertEqual(len(delays), 1)
        self.assertLessEqual(sum(delays), 30)

    def test_conflicts_still_retry_harder_than_a_cap(self):
        ks = _ks()
        self.assertGreater(
            len(ks.KaggleService.CONFLICT_RETRY_DELAYS),
            len(ks.KaggleService.SESSION_CAP_RETRY_DELAYS),
        )

    def test_ladders_are_shared_with_the_stop_path(self):
        ks = _ks()
        self.assertTrue(ks.KaggleService.SESSION_CAP_RETRY_DELAYS)
        self.assertTrue(ks.KaggleService.CONFLICT_RETRY_DELAYS)


class TestCapPushFailsFast(unittest.TestCase):

    def test_cap_push_returns_session_cap_status(self):
        ks, _slept = _run_against_capped_cli()

        self.assertFalse(ks["success"])
        self.assertEqual(ks["status"], "session_cap")
        self.assertIn("batch GPU session limit", ks["message"])

    def test_cap_push_retries_exactly_once(self):
        _, slept = _run_against_capped_cli()
        self.assertEqual(len(slept), 1)
        self.assertLessEqual(sum(slept), 30, "cap must not sleep through a ladder")

    def test_cap_push_does_not_claim_success(self):
        ks, _ = _run_against_capped_cli()
        self.assertNotEqual(ks.get("status"), "queued")


def _capped_cli(stub):
    line = (
        "Kernel push error: 400 - maximum batch GPU session count reached "
        "for this account"
    )
    if os.name == "nt":
        with open(stub, "w") as f:
            f.write(f"@echo off\r\necho {line}\r\nexit /b 1\r\n")
    else:
        with open(stub, "w") as f:
            f.write(f'#!/bin/bash\necho "{line}"\nexit 1\n')
        os.chmod(stub, 0o755)
    return stub


def _run_against_capped_cli():
    import asyncio

    import config as cfg
    import database as db

    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(str(cfg.DB_PATH) + suffix)
        except OSError:
            pass
    importlib.reload(cfg)
    importlib.reload(db)
    import services.kaggle_service as ksmod

    importlib.reload(ksmod)
    stub = _capped_cli(
        os.path.join(DATA_TMP, "capped_kaggle" + (".bat" if os.name == "nt" else ""))
    )
    ksmod.get_kaggle_cli_path = lambda: stub

    async def _noop_stream(*a, **k):
        return

    ksmod.KaggleService.start_background_log_stream = staticmethod(_noop_stream)

    slept: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay, *a, **k):
        slept.append(delay)
        return await real_sleep(0)

    ksmod.asyncio.sleep = fake_sleep
    db.init_db()
    try:
        result = asyncio.run(
            ksmod.KaggleService.push_kernel(
                account_username="acct",
                title="Cap probe",
                code_content="print('hi')",
                filename="probe.py",
                accelerator="nvidia-tesla-t4-x2",
            )
        )
    finally:
        ksmod.asyncio.sleep = real_sleep
    return result, slept


class TestLaunchPreflightGate(unittest.TestCase):

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


class TestPhantomVersionHoldsSlot(unittest.TestCase):

    def test_cancel_acknowledged_is_not_a_holder(self):
        from services import availability

        self.assertNotIn(
            "cancel_acknowledged", availability.SLOT_HOLDING_VERSION_STATUSES
        )
        for held in ("running", "queued", "starting", "pending"):
            self.assertIn(held, availability.SLOT_HOLDING_VERSION_STATUSES)

    def test_probe_reports_running_versions_only(self):
        import database
        import services.kaggle_outputs as ko
        from services import availability

        async def fake_versions(account, ref, max_versions=20):
            return {
                "versions": [
                    {"version": 3, "label": "v3", "status": "running"},
                    {"version": 2, "label": "v2", "status": "cancel_acknowledged"},
                    {"version": 1, "label": "v1", "status": "running"},
                    {"version": 0, "label": "v0", "status": "complete"},
                ]
            }

        orig_refs = database.get_gpu_kernel_refs_for_account
        orig_probe = ko.list_kernel_versions
        database.get_gpu_kernel_refs_for_account = lambda a, limit=12: ["acct/ghost"]
        ko.list_kernel_versions = fake_versions
        try:
            held = asyncio.run(availability.held_version_sessions("acct"))
        finally:
            database.get_gpu_kernel_refs_for_account = orig_refs
            ko.list_kernel_versions = orig_probe

        self.assertEqual([h["version"] for h in held], [3, 1])
        self.assertTrue(all(h["kernel_ref"] == "acct/ghost" for h in held))

    def test_cap_message_names_the_phantom_version(self):
        runs = _runs()
        held = [
            {"kernel_ref": "acct/ghost", "version": 1, "label": "v1", "status": "running"}
        ]

        async def fake_held(account, max_versions=5):
            return held

        orig = runs.availability.held_version_sessions
        runs.availability.held_version_sessions = fake_held
        try:
            msg = asyncio.run(runs._cap_error_message("acct", 2))
        finally:
            runs.availability.held_version_sessions = orig

        self.assertIn("acct/ghost v1 (running)", msg)
        self.assertIn("kaggle kernels delete", msg)

    def test_no_held_versions_leaves_the_base_message_alone(self):
        from services import availability

        self.assertEqual(availability.format_held_sessions([]), "")
        note = availability.format_held_sessions(
            [{"kernel_ref": "a/b", "version": 4, "label": "v4", "status": "running"}]
        )
        self.assertIn("a/b v4 (running)", note)


if __name__ == "__main__":
    unittest.main()
