"""Tests for stop-time recovery of a cancelled version's partial output.

Kaggle finalizes EVERY version (complete/error/cancelled) with its own
output snapshot. Stopping pushes an exit-stub that becomes the LATEST
version, so latest-only pulls return just the stub's log. stop_kernel must
capture the exact cancelled version via version_label instead.
"""

import os
import shutil
import sys
import tempfile
import unittest

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TEST_DIR)

DATA_TMP = None


def _make_stub_cli():
    if os.name == "nt":
        stub = os.path.join(DATA_TMP, "fake_kaggle.bat")
        with open(stub, "w") as f:
            f.write("@echo off\r\nexit /b 0\r\n")
    else:
        stub = os.path.join(DATA_TMP, "fake_kaggle")
        with open(stub, "w") as f:
            f.write("#!/bin/bash\nexit 0\n")
        os.chmod(stub, 0o755)
    return stub


def setUpModule():
    global DATA_TMP
    DATA_TMP = tempfile.mkdtemp(prefix="stop_capture_")
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
    ks = importlib.reload(__import__("services.kaggle_service", fromlist=["x"]))
    stub = _make_stub_cli()
    ks.get_kaggle_cli_path = lambda: stub

    async def _noop_stream(*a, **k):
        return
    ks.KaggleService.start_background_log_stream = staticmethod(_noop_stream)
    db.init_db()
    return cfg, db, ks.KaggleService


def _seed_run(db, run_id, kernel_ref):
    db.create_run_record({
        "id": run_id,
        "account_username": "accA",
        "kernel_slug": kernel_ref.split("/", 1)[1],
        "kernel_ref": kernel_ref,
        "title": "Shard run",
        "code_file": "cell.ipynb",
        "accelerator": "nvidia-tesla-t4-x2",
        "enable_internet": 1,
        "is_trial": 0,
        "timeout_seconds": 43200,
        "status": "running",
        "status_message": "",
        "start_time": "2026-01-01T00:00:00+00:00",
        "kaggle_url": "https://www.kaggle.com/code/" + kernel_ref,
        "workload_id": None,
        "shard_index": None,
        "total_shards": None,
        "log_file": "",
    })


class TestStopOutputCapture(unittest.TestCase):
    def test_1_stop_recovers_cancelled_version_output(self):
        cfg, db, KS = _fresh()
        _seed_run(db, "runX", "accA/shard-run")

        captured = {}

        async def fake_version(account, ref):
            captured["version_lookup"] = (account, ref)
            return 7

        async def fake_plain_download(account, ref, run_id):
            # Latest pull before the stub: creates empty dir, no files
            from pathlib import Path as _P

            d = os.path.join(cfg.OUTPUTS_DIR, run_id)
            os.makedirs(d, exist_ok=True)
            return _P(d)

        async def fake_versioned_download(account, ref, version, run_id):
            from pathlib import Path as _P

            d = os.path.join(cfg.OUTPUTS_DIR, run_id)
            os.makedirs(d, exist_ok=True)
            with open(  # noqa: ASYNC230 - tiny fixture write inside an async stand-in
                os.path.join(d, "task_a_labeled_shard_0.jsonl"), "w"
            ) as f:
                f.write('{"id": "recovered"}\n')
            captured["versioned"] = (account, ref, version, run_id)
            return _P(d)

        KS.get_kernel_current_version = staticmethod(fake_version)
        KS.download_outputs = staticmethod(fake_plain_download)
        KS.download_outputs_of_version = staticmethod(fake_versioned_download)
        KS.STOP_CAPTURE_RETRY_DELAYS = (0,)  # instant in tests

        res = asyncio_run(KS.stop_kernel("runX"))
        self.assertTrue(res["success"], res)
        self.assertEqual(captured["version_lookup"], ("accA", "accA/shard-run"))
        self.assertEqual(captured["versioned"], ("accA", "accA/shard-run", 7, "runX"))
        marker = os.path.join(cfg.OUTPUTS_DIR, "runX", "task_a_labeled_shard_0.jsonl")
        self.assertTrue(os.path.isfile(marker))
        self.assertEqual(db.get_run_by_id("runX")["status"], "stopped")
        # The recovered version must be PINNED so later manual pulls skip the stub
        self.assertEqual(db.get_run_by_id("runX")["output_version"], 7)

    def test_2_unknown_version_still_stops_cleanly(self):
        cfg, db, KS = _fresh()
        _seed_run(db, "runY", "accB/shard-run-y")

        async def no_version(account, ref):
            return None

        async def plain(account, ref, run_id):
            from pathlib import Path as _P

            d = os.path.join(cfg.OUTPUTS_DIR, run_id)
            os.makedirs(d, exist_ok=True)
            return _P(d)

        calls = {"versioned": 0}

        async def versioned(account, ref, version, run_id):
            calls["versioned"] += 1

        KS.get_kernel_current_version = staticmethod(no_version)
        KS.download_outputs = staticmethod(plain)
        KS.download_outputs_of_version = staticmethod(versioned)

        res = asyncio_run(KS.stop_kernel("runY"))
        self.assertTrue(res["success"])
        self.assertEqual(calls["versioned"], 0)  # nothing to recover - never invoked
        self.assertEqual(db.get_run_by_id("runY")["status"], "stopped")


def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)


if __name__ == "__main__":
    unittest.main(verbosity=2)
