import os
import shutil
import sys
import tempfile
import unittest
from datetime import UTC

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TEST_DIR)

DATA_TMP = None


_STUB_CLI = None


def _make_stub_cli() -> str:
    global _STUB_CLI
    if _STUB_CLI and os.path.exists(_STUB_CLI):
        return _STUB_CLI
    if os.name == "nt":
        stub = os.path.join(DATA_TMP, "fake_kaggle.bat")
        with open(stub, "w") as f:
            f.write("@echo off\r\nexit /b 0\r\n")
    else:
        stub = os.path.join(DATA_TMP, "fake_kaggle")
        with open(stub, "w") as f:
            f.write("#!/bin/bash\nexit 0\n")
        os.chmod(stub, 0o755)
    _STUB_CLI = stub
    return stub


def setUpModule():
    global DATA_TMP
    DATA_TMP = tempfile.mkdtemp(prefix="cap_harness_")
    os.environ["AUTOMATION_DATA_DIR"] = DATA_TMP
    os.environ["INTER_PUSH_STAGGER_SECONDS"] = "0"

    import services.kaggle_service as ks

    ks.get_kaggle_cli_path = lambda: _make_stub_cli()

    async def _noop_stream(*a, **k):
        return

    ks.KaggleService.start_background_log_stream = staticmethod(_noop_stream)


def tearDownModule():
    if DATA_TMP and os.path.isdir(DATA_TMP):
        shutil.rmtree(DATA_TMP, ignore_errors=True)
    os.environ.pop("AUTOMATION_DATA_DIR", None)
    os.environ.pop("INTER_PUSH_STAGGER_SECONDS", None)


def _fresh():
    import importlib

    import config as cfg
    import database as db
    import services.kaggle_service as ks

    db_file = cfg.DB_PATH
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(str(db_file) + suffix)
        except OSError:
            pass
    importlib.reload(cfg)
    importlib.reload(db)
    importlib.reload(ks)
    ks.get_kaggle_cli_path = lambda: _make_stub_cli()

    async def _noop_stream(*a, **k):
        return

    ks.KaggleService.start_background_log_stream = staticmethod(_noop_stream)
    db.init_db()
    return db


def _seed_account(db, username, used, limit, error=False):
    quota = {
        "gpu": {
            "name": "GPU (T4 x 2)",
            "used": used,
            "limit": limit,
            "unit": "hours",
            "percent": round(used / limit * 100, 1),
        },
        "tpu": {
            "name": "TPU VM v3-8",
            "used": 0,
            "limit": 20,
            "unit": "hours",
            "percent": 0,
        },
    }
    if error:
        quota["error"] = "quota lookup timed out"
    db.save_account(f"id_{username}", username, f"key_{username}", quota)


def _gpu_kwargs(title="CapTest", account="accA", **over):
    kw = {
        "base_title": title,
        "code_content": 'print("x")',
        "filename": "main.py",
        "accounts": [account],
        "total_items": 10,
        "accelerator": "nvidia-tesla-t4-x2",
        "sessions_per_account": 1,
    }
    kw.update(over)
    return kw


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)


class TestBudgetMath(unittest.TestCase):

    def _compute(self, used, limit, concurrent):
        from services.account_manager import AccountManager

        return AccountManager.compute_gpu_runtime_budget_minutes(
            used, limit, concurrent
        )

    def test_split_remaining_across_concurrent_sessions(self):
        self.assertEqual(self._compute(20, 30, 1), 530)
        self.assertEqual(self._compute(20, 30, 2), 230)
        self.assertEqual(self._compute(20, 30, 3), 130)

    def test_never_caps_when_remaining_is_at_least_12h_per_session(self):
        self.assertIsNone(self._compute(0, 30, 1))
        self.assertIsNone(self._compute(0, 30, 2))
        self.assertIsNone(self._compute(1, 30, 1))
        self.assertIsNone(self._compute(17, 30, 1))
        self.assertIsNone(self._compute(18, 30, 1))
        self.assertEqual(self._compute(0, 30, 3), 530)

    def test_caps_below_12h_boundary(self):
        self.assertEqual(self._compute(19, 30, 1), 590)
        self.assertIn(self._compute(18.1, 30, 1), (643, 644))
        self.assertIsNone(self._compute(18.0, 30, 1))
        self.assertEqual(self._compute(18.01, 30, 1), 649)

    def test_spent_or_tiny_remaining_no_cap(self):
        self.assertIsNone(self._compute(30, 30, 1))
        self.assertIsNone(self._compute(29.6, 30, 2))
        self.assertIsNone(self._compute(29.5, 30, 1))
        self.assertIsNone(self._compute(29.2, 30, 1))

    def test_allowance_applied(self):
        self.assertEqual(self._compute(28, 30, 1), 50)
        self.assertEqual(self._compute(28.5, 30, 1), 20)

    def test_garbage_input_never_caps(self):
        self.assertIsNone(self._compute(None, 30, 1))
        self.assertIsNone(self._compute(5, None, 1))
        self.assertIsNone(self._compute(5, 0, 1))
        self.assertIsNone(self._compute(-1, 30, 1))
        self.assertIsNone(self._compute("n/a", "x", 1))

    def test_budget_never_exceeds_scripts_11h_ceiling(self):
        from services.account_manager import AccountManager

        for used_tenths in range(1801, 3000, 37):
            used = used_tenths / 100.0
            for burners in (1, 2, 3):
                budget = AccountManager.compute_gpu_runtime_budget_minutes(
                    used, 30, burners
                )
                if budget is not None:
                    self.assertLess(budget, 660, f"used={used} burners={burners}")
                    remaining = (30 - used) * 60.0 / burners
                    self.assertLess(
                        60 + budget,
                        remaining,
                        f"used={used} burners={burners} budget={budget}",
                    )


class TestTimingMatrix(unittest.TestCase):

    def test_exit_always_precedes_quota_death(self):
        from services.account_manager import PRE_LOOP_ALLOWANCE_MINUTES, AccountManager

        final_item_min = 2
        cold_starts = [0, 5, 11, 30, 45, 59.9]
        checked_capped = 0
        checked_uncapped = 0
        for remaining_h in [x / 100.0 for x in range(1, 3001)]:
            used = 30 - remaining_h
            if used < 0:
                continue
            for burners in (1, 2, 3):
                runway_min = remaining_h * 60.0 / burners
                budget = AccountManager.compute_gpu_runtime_budget_minutes(
                    used, 30, burners
                )
                if budget is None:
                    if runway_min >= 720:
                        checked_uncapped += 1
                    continue
                checked_capped += 1
                for L in cold_starts:
                    self.assertLessEqual(
                        L,
                        PRE_LOOP_ALLOWANCE_MINUTES,
                        "test assumes cold starts within the 60-min allowance",
                    )
                    exit_session_min = L + budget + final_item_min
                    self.assertLess(
                        exit_session_min,
                        runway_min,
                        f"remaining={remaining_h}h burners={burners} L={L}min "
                        f"budget={budget}: exit {exit_session_min:.1f}min "
                        f"not before quota death {runway_min:.1f}min",
                    )
        self.assertGreater(
            checked_capped, 500, "matrix must meaningfully exercise caps"
        )
        self.assertGreater(
            checked_uncapped, 500, "matrix must exercise the no-cap zone"
        )


def _age_account(db, username, minutes):
    from datetime import datetime, timedelta

    from database import get_db_connection

    old = (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat()
    conn = get_db_connection()
    with conn:
        conn.execute(
            "UPDATE accounts SET last_checked = ? WHERE username = ?", (old, username)
        )
    conn.close()


def _patch_refresh(testcase, fake):
    import services.account_manager as am

    orig = am.AccountManager.refresh_account_quota.__func__
    am.AccountManager.refresh_account_quota = staticmethod(fake)
    testcase.addCleanup(
        setattr, am.AccountManager, "refresh_account_quota", classmethod(orig)
    )


class TestAccountLookup(unittest.TestCase):

    def _budget(self, username, concurrent):
        from services.account_manager import AccountManager

        return asyncio_run(
            AccountManager.gpu_runtime_budget_minutes(username, concurrent)
        )

    def test_missing_account_no_cap(self):
        _fresh()
        self.assertIsNone(self._budget("ghost", 1))

    def test_failed_lookup_never_caps(self):
        db = _fresh()
        _seed_account(db, "accA", used=29, limit=30, error=True)
        self.assertIsNone(self._budget("accA", 1))

    def test_disabled_flag_no_cap(self):
        db = _fresh()
        _seed_account(db, "accA", used=20, limit=30)
        import services.account_manager as am

        saved = am.QUOTA_CAP_ENABLED
        try:
            am.QUOTA_CAP_ENABLED = False
            self.assertIsNone(self._budget("accA", 1))
        finally:
            am.QUOTA_CAP_ENABLED = saved

    def test_happy_path_fresh_row_no_cli(self):
        db = _fresh()
        _seed_account(db, "accA", used=20, limit=30)
        calls = []

        async def boom(*a, **k):
            calls.append(a)
            raise AssertionError("fresh row must not trigger a live quota refresh")

        _patch_refresh(self, boom)
        self.assertEqual(self._budget("accA", 1), 530)
        self.assertEqual(calls, [])

    def test_stale_row_refreshes_live(self):
        db = _fresh()
        _seed_account(db, "accA", used=20, limit=30)
        _age_account(db, "accA", minutes=90)
        calls = []

        async def fake_refresh(username):
            calls.append(username)
            return {
                "gpu": {"used": 25, "limit": 30, "unit": "hours", "percent": 83.3},
                "tpu": {"used": 0, "limit": 20, "unit": "hours", "percent": 0},
            }

        _patch_refresh(self, fake_refresh)
        self.assertEqual(self._budget("accA", 1), 230)
        self.assertEqual(calls, ["accA"])

    def test_refresh_failure_falls_back_to_stored(self):
        db = _fresh()
        _seed_account(db, "accA", used=20, limit=30)
        _age_account(db, "accA", minutes=90)

        async def boom(*a, **k):
            raise RuntimeError("kaggle CLI down")

        _patch_refresh(self, boom)
        self.assertEqual(self._budget("accA", 1), 530)

    def test_barely_fresh_row_skips_refresh(self):
        db = _fresh()
        _seed_account(db, "accA", used=20, limit=30)
        _age_account(db, "accA", minutes=14)
        calls = []

        async def boom(*a, **k):
            calls.append(a)
            raise AssertionError("14-min-old row must not trigger a live refresh")

        _patch_refresh(self, boom)
        self.assertEqual(self._budget("accA", 1), 530)
        self.assertEqual(calls, [])


def _patch_push(testcase, module_obj):
    KS = module_obj.KaggleService
    bound = KS.push_kernel
    orig_fn = (
        getattr(bound, "__func__", None) or bound
    )
    captured = []

    async def fake_push(**kw):
        captured.append(kw)
        return {"success": True, "run_id": "run_x", "kernel_ref": "acc/x"}

    KS.push_kernel = staticmethod(fake_push)
    testcase.addCleanup(setattr, KS, "push_kernel", classmethod(orig_fn))
    return captured


class TestDistributorInjection(unittest.TestCase):

    def _launch(
        self, db, account="accA", accelerator="nvidia-tesla-t4-x2", env_vars=None
    ):
        import services.workload_distributor as wd

        captured = _patch_push(self, wd)
        res = asyncio_run(
            wd.WorkloadDistributor.distribute_and_launch(
                **_gpu_kwargs(
                    account=account, accelerator=accelerator, env_vars=env_vars
                )
            )
        )
        return res, captured

    def test_gpu_launch_injects_quota_cap(self):
        db = _fresh()
        _seed_account(db, "accA", used=20, limit=30)
        res, captured = self._launch(db)
        self.assertTrue(res["success"], res)
        self.assertEqual(res["shards"][0]["runtime_budget_minutes"], 530)
        self.assertEqual(captured[0]["env_vars"]["MAX_RUNTIME_MINUTES"], "530")
        self.assertEqual(res["runner_plan"][0]["runtime_budget_minutes"], 530)

    def test_user_pinned_env_wins(self):
        db = _fresh()
        _seed_account(db, "accA", used=20, limit=30)
        res, captured = self._launch(db, env_vars={"MAX_RUNTIME_MINUTES": "123"})
        self.assertTrue(res["success"])
        self.assertEqual(captured[0]["env_vars"]["MAX_RUNTIME_MINUTES"], "123")

    def test_cpu_launch_no_cap(self):
        db = _fresh()
        _seed_account(db, "accA", used=20, limit=30)
        res, captured = self._launch(db, accelerator="none")
        self.assertTrue(res["success"])
        self.assertIsNone(captured[0]["env_vars"])
        self.assertIsNone(res["shards"][0]["runtime_budget_minutes"])

    def test_generous_quota_no_cap(self):
        db = _fresh()
        _seed_account(db, "accA", used=5, limit=30)
        res, captured = self._launch(db)
        self.assertTrue(res["success"])
        self.assertIsNone(captured[0]["env_vars"])
        self.assertIsNone(res["shards"][0]["runtime_budget_minutes"])

    def test_two_sessions_share_the_remaining_quota(self):
        db = _fresh()
        _seed_account(db, "accA", used=20, limit=30)
        import services.workload_distributor as wd

        captured = _patch_push(self, wd)
        res = asyncio_run(
            wd.WorkloadDistributor.distribute_and_launch(
                **_gpu_kwargs(sessions_per_account=2)
            )
        )
        self.assertTrue(res["success"])
        self.assertEqual(len(captured), 2)
        for kw in captured:
            self.assertEqual(kw["env_vars"]["MAX_RUNTIME_MINUTES"], "230")


class TestSingleRunInjection(unittest.TestCase):

    def _launch(
        self, db, account="accA", accelerator="nvidia-tesla-t4-x2", env_vars=None
    ):
        import routers.runs as R

        captured = _patch_push(self, R)
        req = R.LaunchRunJSONRequest(
            account_username=account,
            title="T",
            code_content='print("x")',
            filename="main.py",
            accelerator=accelerator,
            env_vars=env_vars,
        )
        asyncio_run(R.launch_run_json(req))
        return captured

    def test_gpu_launch_injects(self):
        db = _fresh()
        _seed_account(db, "accA", used=20, limit=30)
        captured = self._launch(db)
        self.assertEqual(captured[0]["env_vars"]["MAX_RUNTIME_MINUTES"], "530")

    def test_active_gpu_run_shares_the_budget(self):
        db = _fresh()
        _seed_account(db, "accA", used=20, limit=30)
        db.create_run_record(
            {
                "id": "run_seed",
                "account_username": "accA",
                "kernel_slug": "busy",
                "kernel_ref": "accA/busy",
                "title": "Busy",
                "code_file": "x.py",
                "accelerator": "nvidia-tesla-t4-x2",
                "enable_internet": 1,
                "is_trial": 0,
                "timeout_seconds": 43200,
                "status": "running",
                "status_message": "",
                "start_time": "2026-08-23T00:00:00+00:00",
                "kaggle_url": "https://www.kaggle.com/code/accA/busy",
                "workload_id": None,
                "shard_index": None,
                "total_shards": None,
                "log_file": "",
            }
        )
        captured = self._launch(db)
        self.assertEqual(captured[0]["env_vars"]["MAX_RUNTIME_MINUTES"], "230")

    def test_pinned_env_wins_and_cpu_skips(self):
        db = _fresh()
        _seed_account(db, "accA", used=20, limit=30)
        captured = self._launch(db, env_vars={"MAX_RUNTIME_MINUTES": "45"})
        self.assertEqual(captured[0]["env_vars"]["MAX_RUNTIME_MINUTES"], "45")
        captured = self._launch(db, accelerator="none")
        self.assertIsNone(captured[0]["env_vars"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
