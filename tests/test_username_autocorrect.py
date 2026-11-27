import os
import shutil
import sys
import tempfile
import unittest

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TEST_DIR)

DATA_TMP = None
STUB_COUNT_FILE = None
STUB_PATH = None


def _write_stub() -> str:
    if os.name == "nt":
        stub = os.path.join(DATA_TMP, "fake_kaggle.bat")
        content = (
            "@echo off\r\n"
            'if not "%1"=="kernels" exit /b 0\r\n'
            'if not "%2"=="push" exit /b 0\r\n'
            'if "%KAGGLE_API_TOKEN%"=="" (\r\n'
            "  echo Authentication required to call the Kaggle API. 1>&2\r\n"
            "  echo Authentication required to call the Kaggle API. 1>&2\r\n"
            "  exit /b 1\r\n"
            ")\r\n"
            'set "cf=%KAGGLE_STUB_COUNTFILE%"\r\n'
            'if not defined cf set "cf=%TEMP%\\kaggle_stub_count.txt"\r\n'
            'if not exist "%cf%" echo 0> "%cf%"\r\n'
            'set /p count= < "%cf%"\r\n'
            "set /a count+=1\r\n"
            '> "%cf%" echo %count%\r\n'
            'if "%KAGGLE_STUB_OWNER%"=="" (set "owner=owner_x") else (set "owner=%KAGGLE_STUB_OWNER%")\r\n'
            "echo Kernel Push success: https://www.kaggle.com/code/%owner%/dist-stub-shard-%count%\r\n"
            "exit /b 0\r\n"
        )
    else:
        stub = os.path.join(DATA_TMP, "fake_kaggle")
        content = (
            "#!/bin/bash\n"
            'if [ "$1" = "kernels" ] && [ "$2" = "push" ]; then\n'
            '  if [ -z "${KAGGLE_API_TOKEN}" ]; then\n'
            '    echo "Authentication required to call the Kaggle API." >&2\n'
            '    echo "Authentication required to call the Kaggle API." >&2\n'
            "    exit 1\n"
            "  fi\n"
            '  countfile="${KAGGLE_STUB_COUNTFILE:-/tmp/kaggle_stub_count.txt}"\n'
            "  count=0\n"
            '  [ -f "$countfile" ] && count=$(cat "$countfile")\n'
            "  count=$((count + 1))\n"
            '  echo "$count" > "$countfile"\n'
            '  owner="${KAGGLE_STUB_OWNER:-owner_x}"\n'
            '  echo "Kernel Push success: https://www.kaggle.com/code/${owner}/dist-stub-shard-${count}"\n'
            "  exit 0\n"
            "fi\n"
            "exit 0\n"
        )
    with open(stub, "w") as f:
        f.write(content)
    if os.name != "nt":
        os.chmod(stub, 0o755)
    return stub


def setUpModule():
    global DATA_TMP, STUB_COUNT_FILE, STUB_PATH
    DATA_TMP = tempfile.mkdtemp(prefix="autocorrect_harness_")
    STUB_COUNT_FILE = os.path.join(DATA_TMP, "stub_count.txt")
    STUB_PATH = _write_stub()
    os.environ["AUTOMATION_DATA_DIR"] = DATA_TMP
    os.environ["INTER_PUSH_STAGGER_SECONDS"] = "0"
    with open(STUB_COUNT_FILE, "w") as f:
        f.write("0")


def tearDownModule():
    if DATA_TMP and os.path.isdir(DATA_TMP):
        shutil.rmtree(DATA_TMP, ignore_errors=True)
    os.environ.pop("AUTOMATION_DATA_DIR", None)
    os.environ.pop("INTER_PUSH_STAGGER_SECONDS", None)


def _fresh():
    import importlib

    import config as cfg
    import database as db
    import services.account_manager as am
    import services.kaggle_service as ks

    db_file = cfg.DB_PATH
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(str(db_file) + suffix)
        except OSError:
            pass

    importlib.reload(cfg)
    importlib.reload(db)
    importlib.reload(am)
    importlib.reload(ks)
    ks.get_kaggle_cli_path = lambda: STUB_PATH
    am.get_kaggle_cli_path = lambda: STUB_PATH

    async def _noop_stream(*a, **k):
        return
    ks.KaggleService.start_background_log_stream = staticmethod(_noop_stream)

    with open(STUB_COUNT_FILE, "w") as f:
        f.write("0")
    db.init_db()
    return db, am


def _asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)


class TestUsernameAutoCorrect(unittest.TestCase):
    def test_alias_env_and_db_survive_rename(self):
        import config as cfg
        db, am = _fresh()
        db.save_account("id1", "kaggle_abc123", "tok_abc", None)
        am.AccountManager.setup_account_files("kaggle_abc123", "tok_abc")

        am.AccountManager.update_username("kaggle_abc123", "realuser1")

        self.assertEqual(am.AccountManager.resolve_effective_username("kaggle_abc123"), "realuser1")
        self.assertEqual(am.AccountManager.resolve_effective_username("realuser1"), "realuser1")
        env = am.AccountManager.get_account_env("kaggle_abc123")
        self.assertEqual(env.get("KAGGLE_API_TOKEN"), "tok_abc")
        self.assertIsNone(db.get_account_by_username("kaggle_abc123"))
        self.assertEqual(db.get_account_by_username("realuser1")["api_key"], "tok_abc")
        self.assertTrue(cfg.ACCOUNTS_DIR.joinpath("kaggle_abc123", ".kaggle", "access_token").exists())

    def test_two_gpu_sessions_survive_mid_batch_auto_correct(self):
        db, am = _fresh()
        import app.services.workload_distributor as wd
        importlib = __import__("importlib")
        importlib.reload(wd)
        os.environ["KAGGLE_STUB_OWNER"] = "owner_x"
        os.environ["KAGGLE_STUB_COUNTFILE"] = STUB_COUNT_FILE
        try:
            am.AccountManager.setup_account_files("kaggle_hash1", "tok_x")

            res = _asyncio_run(wd.WorkloadDistributor.distribute_and_launch(
                base_title="FreshAcc",
                code_content='print("shard")',
                filename="main.py",
                accounts=["kaggle_hash1"],
                total_items=10,
                accelerator="nvidia-tesla-t4-x2",
                sessions_per_account=2,
            ))
        finally:
            os.environ.pop("KAGGLE_STUB_OWNER", None)
            os.environ.pop("KAGGLE_STUB_COUNTFILE", None)

        self.assertTrue(res["success"], res)
        self.assertEqual(res["total_shards"], 2)
        self.assertEqual(res["shards_pushed"], 2)
        self.assertEqual(res["status"], "dispatched")

        runs = db.get_all_runs()
        self.assertEqual(len(runs), 2)
        for r in runs:
            self.assertEqual(r["account_username"], "owner_x")
            self.assertTrue(r["kernel_ref"].startswith("owner_x/"), r["kernel_ref"])

    def test_stop_kernel_globally_throttled(self):
        import asyncio

        import services.kaggle_service as ks
        _db, _am = _fresh()
        ks.KaggleService.STOP_CONCURRENCY_LIMIT = 2

        state = {"active": 0, "max": 0, "calls": 0}

        async def fake_impl(run_id):
            state["active"] += 1
            state["max"] = max(state["max"], state["active"])
            await asyncio.sleep(0.02)
            state["active"] -= 1
            state["calls"] += 1
            return {"success": True, "run_id": run_id}

        orig = ks.KaggleService._stop_kernel_impl
        ks.KaggleService._stop_kernel_impl = staticmethod(fake_impl)
        try:
            async def go():
                await asyncio.gather(*[ks.KaggleService.stop_kernel(f"run{i}") for i in range(8)])
            asyncio.run(go())
        finally:
            ks.KaggleService._stop_kernel_impl = orig

        self.assertEqual(state["calls"], 8)
        self.assertLessEqual(state["max"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
