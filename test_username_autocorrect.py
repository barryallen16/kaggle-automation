"""Hermetic regression tests for the account username auto-correct race.

Bug: adding a NEW Kaggle account leaves a placeholder username in the DB
(kaggle_<hash>). The first push of a launch batch discovers the real username
from Kaggle's response and renames the account row MID-BATCH - the second GPU
session of the same account was already planned under the stale placeholder
name and its token file had been deleted by the rename, so its push died with
"Authentication required to call the Kaggle API.".

Also covers the Stop-All OOM: every stop_kernel now runs under a global
semaphore so a 36-shard workload cannot spawn 36 heavyweight stop pipelines at
once.

Uses a stub CLI + temp data dir - nothing touches production or real Kaggle.
"""

import os
import sys
import json
import shutil
import tempfile
import unittest

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TEST_DIR)

DATA_TMP = None
STUB_COUNT_FILE = None
STUB_PATH = None


def _write_stub() -> str:
    """Stub `kaggle` CLI.

    - `kernels push`: AUTHENTICATES only when KAGGLE_API_TOKEN is set (mirrors
      the real CLI). On success prints a Kaggle URL owned by KAGGLE_STUB_OWNER
      (the "real username" Kaggle reveals on a successful push) with a unique
      slug per push so consecutive pushes create distinct kernel refs.
    - anything else: silent success.
    """
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
    """Fresh DB + modules for each test (config paths bind at import)."""
    import importlib
    import app.config as cfg
    import app.database as db
    import app.services.account_manager as am
    import app.services.kaggle_service as ks

    db_file = cfg.DB_PATH
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(str(db_file) + suffix)
        except OSError:
            pass

    # Reload order matters: ks/wd import AccountManager by name, so the manager
    # (and its in-memory username-alias map) must be fresh before they reload.
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
        """update_username keeps old credentials readable + registers an alias."""
        import app.config as cfg
        db, am = _fresh()
        db.save_account("id1", "kaggle_abc123", "tok_abc", None)
        am.AccountManager.setup_account_files("kaggle_abc123", "tok_abc")

        am.AccountManager.update_username("kaggle_abc123", "realuser1")

        # Old name resolves to the current one
        self.assertEqual(am.AccountManager.resolve_effective_username("kaggle_abc123"), "realuser1")
        self.assertEqual(am.AccountManager.resolve_effective_username("realuser1"), "realuser1")
        # Env built under the STALE name still carries the token (the old
        # config dir was mirrored, not deleted - a second shard of the same
        # batch must not hit "Authentication required").
        env = am.AccountManager.get_account_env("kaggle_abc123")
        self.assertEqual(env.get("KAGGLE_API_TOKEN"), "tok_abc")
        # DB row moved to the real username
        self.assertIsNone(db.get_account_by_username("kaggle_abc123"))
        self.assertEqual(db.get_account_by_username("realuser1")["api_key"], "tok_abc")
        self.assertTrue(cfg.ACCOUNTS_DIR.joinpath("kaggle_abc123", ".kaggle", "access_token").exists())

    def test_two_gpu_sessions_survive_mid_batch_auto_correct(self):
        """Fresh account (placeholder name) + sessions=2: BOTH pushes succeed.

        Before the fix the first push renamed kaggle_<hash> -> owner_x and
        deleted the old config dir; the second session then failed auth and the
        workload only reached 'partial' with 1 shard.
        """
        db, am = _fresh()
        import app.services.workload_distributor as wd
        importlib = __import__("importlib")
        importlib.reload(wd)
        # Stub reveals the account's REAL username on the first push.
        os.environ["KAGGLE_STUB_OWNER"] = "owner_x"
        os.environ["KAGGLE_STUB_COUNTFILE"] = STUB_COUNT_FILE
        try:
            # Seed a token file for the placeholder account (no DB row needed).
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
        """Stop-All fans out over every active shard; the pipeline stays bounded."""
        import asyncio
        import app.services.kaggle_service as ks
        db, am = _fresh()
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
