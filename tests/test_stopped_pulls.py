"""Tests for output pulls on stopped runs.

After a stop, the kernel's LATEST version is the exit-stub (log only). The
run's real partial output lives on the CANCELLED version. Pull paths must:
  1. prefer the run's pinned output_version,
  2. rescue legacy unpinned stopped runs by probing (current - 1),
  3. persist a rescued pin, and
  4. never fall back to the stub when real version data exists.
"""

import os
import shutil
import sys
import tempfile
import unittest

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TEST_DIR)

DATA_TMP = None


def setUpModule():
    global DATA_TMP
    DATA_TMP = tempfile.mkdtemp(prefix="pull_pref_")
    os.environ["AUTOMATION_DATA_DIR"] = DATA_TMP


def tearDownModule():
    if DATA_TMP and os.path.isdir(DATA_TMP):
        shutil.rmtree(DATA_TMP, ignore_errors=True)
    os.environ.pop("AUTOMATION_DATA_DIR", None)


def _fresh():
    global cfg, db, ks, fr
    import importlib

    import config as _cfg
    import database as _db
    import services.kaggle_service as _ks
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(str(_cfg.DB_PATH) + suffix)
        except OSError:
            pass
    cfg = importlib.reload(_cfg)
    db = importlib.reload(_db)
    ks = importlib.reload(_ks)

    async def _noop(*a, **k):
        return None
    ks.KaggleService.start_background_log_stream = staticmethod(_noop)

    import routers.files as _files
    fr = importlib.reload(_files)
    db.init_db()
    return cfg, db, ks, fr


def _seed_run(db, run_id, status="stopped"):
    db.create_run_record({
        "id": run_id,
        "account_username": "accA",
        "kernel_slug": f"slug-{run_id}",
        "kernel_ref": f"accA/slug-{run_id}",
        "title": f"T-{run_id}",
        "code_file": "",
        "accelerator": "nvidia-tesla-t4-x2",
        "enable_internet": 1,
        "is_trial": 0,
        "timeout_seconds": 300,
        "status": status,
        "status_message": "",
        "start_time": "2026-01-01T00:00:00+00:00",
        "kaggle_url": "u",
        "workload_id": None,
        "shard_index": None,
        "total_shards": None,
        "log_file": "",
    })


class TestStoppedRunPulls(unittest.TestCase):
    def test_1_pinned_version_wins_over_latest(self):
        cfg, db, ks, fr = _fresh()
        _seed_run(db, "pinned", status="stopped")
        db.set_run_output_version("pinned", 3)

        calls = {"versioned": [], "plain": 0, "current_lookup": 0}

        async def fake_current(account, ref):
            calls["current_lookup"] += 1
            return 9

        async def fake_versioned(account, ref, version, run_id):
            calls["versioned"].append(version)
            d = cfg.OUTPUTS_DIR / run_id
            d.mkdir(parents=True, exist_ok=True)
            (d / "shard.jsonl").write_text('{"id":"real"}\n', encoding="utf-8")
            return d

        async def fake_plain(account, ref, run_id):
            calls["plain"] += 1

        ks.KaggleService.get_kernel_current_version = staticmethod(fake_current)
        ks.KaggleService.download_outputs_of_version = staticmethod(fake_versioned)
        ks.KaggleService.download_outputs = staticmethod(fake_plain)

        res = asyncio_run(fr.pull_files_from_kaggle("pinned"))
        self.assertTrue(res["success"])
        self.assertEqual(calls["versioned"], [3])   # pinned version fetched...
        self.assertEqual(calls["plain"], 0)          # ...stub never touched
        self.assertEqual(calls["current_lookup"], 0)  # pin short-circuits lookup
        names = [f["name"] for f in res["files"]]
        self.assertIn("shard.jsonl", names)

    def test_2_legacy_unpinned_stop_rescued_via_previous_version(self):
        cfg, db, ks, fr = _fresh()
        _seed_run(db, "legacy", status="stopped")

        # Current latest = the stop-stub (v9); cancelled run was v8.
        async def fake_current(account, ref):
            return 9

        calls = {"versioned": []}

        async def fake_versioned(account, ref, version, run_id):
            calls["versioned"].append(version)
            if version != 8:
                return None  # stub v9 has nothing but a log -> helper yields no files
            d = cfg.OUTPUTS_DIR / run_id
            d.mkdir(parents=True, exist_ok=True)
            (d / "partial.jsonl").write_text('{"id":1}\n', encoding="utf-8")
            return d

        async def fake_plain(account, ref, run_id):
            raise AssertionError("plain fallback must not run when v8 recovers")

        ks.KaggleService.get_kernel_current_version = staticmethod(fake_current)
        ks.KaggleService.download_outputs_of_version = staticmethod(fake_versioned)
        ks.KaggleService.download_outputs = staticmethod(fake_plain)

        res = asyncio_run(fr.pull_files_from_kaggle("legacy"))
        self.assertTrue(res["success"])
        self.assertEqual(calls["versioned"], [8])  # probed previous FIRST
        # Rescued pin persisted so future pulls skip probing
        self.assertEqual(db.get_run_by_id("legacy")["output_version"], 8)
        self.assertIn("version 8", res["message"])

    def test_3_completed_run_pulls_its_current_version(self):
        cfg, db, ks, fr = _fresh()
        _seed_run(db, "done", status="complete")

        async def fake_current(account, ref):
            return 4

        calls = {"versioned": [], "plain": 0}

        async def fake_versioned(account, ref, version, run_id):
            calls["versioned"].append(version)
            d = cfg.OUTPUTS_DIR / run_id
            d.mkdir(parents=True, exist_ok=True)
            return d

        async def fake_plain(account, ref, run_id):
            calls["plain"] += 1

        ks.KaggleService.get_kernel_current_version = staticmethod(fake_current)
        ks.KaggleService.download_outputs_of_version = staticmethod(fake_versioned)
        ks.KaggleService.download_outputs = staticmethod(fake_plain)

        res = asyncio_run(fr.pull_files_from_kaggle("done"))
        self.assertTrue(res["success"])
        self.assertEqual(calls["versioned"], [4])  # no probe for completed runs
        self.assertEqual(calls["plain"], 0)
        self.assertEqual(db.get_run_by_id("done")["output_version"], 4)


def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)


if __name__ == "__main__":
    unittest.main(verbosity=2)
