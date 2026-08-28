"""Tests for the server-storage clear endpoints.

Covers the notebook-cache clear (push payloads accumulate forever - nothing
reads them back after a push) and pins the shared dir-clear helper behind
all three clear endpoints.
"""

import os
import shutil
import sys
import tempfile
import unittest

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TEST_DIR)

DATA_TMP = tempfile.mkdtemp(prefix="clear_harness_")
os.environ["AUTOMATION_DATA_DIR"] = DATA_TMP


def setUpModule():
    pass


def tearDownModule():
    if os.path.isdir(DATA_TMP):
        shutil.rmtree(DATA_TMP, ignore_errors=True)
    os.environ.pop("AUTOMATION_DATA_DIR", None)


def _fresh():
    """Reload config/database/files modules bound to the temp dir."""
    global cfg, db, files_router
    import importlib

    import config as _cfg
    import database as _db
    cfg = importlib.reload(_cfg)
    db = importlib.reload(_db)
    import routers.files as _files
    files_router = importlib.reload(_files)
    db.init_db()
    return cfg, db, files_router


def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)


class TestClearNotebooks(unittest.TestCase):
    def test_1_clears_run_dirs_and_stop_stubs(self):
        cfg, _db, fr = _fresh()
        run_dir = cfg.NOTEBOOKS_DIR / "run_20250101_000000_abc123"
        stop_dir = cfg.NOTEBOOKS_DIR / "stop_run_20250101_000000_abc123"
        run_dir.mkdir(parents=True, exist_ok=True)
        stop_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "notebook.ipynb").write_text("{}", encoding="utf-8")
        (run_dir / "kernel-metadata.json").write_text("{}", encoding="utf-8")
        (stop_dir / "cell.ipynb").write_text("{}", encoding="utf-8")

        resp = asyncio_run(fr.clear_all_notebooks())

        self.assertTrue(resp["success"])
        self.assertIn("2", resp["message"])
        self.assertFalse(run_dir.exists())
        self.assertFalse(stop_dir.exists())

    def test_2_empty_dir_succeeds_with_zero_count(self):
        _cfg, _db, fr = _fresh()
        resp = asyncio_run(fr.clear_all_notebooks())
        self.assertTrue(resp["success"])
        self.assertIn("0", resp["message"])

    def test_3_shared_helper_still_clears_outputs_and_logs(self):
        cfg, _db, fr = _fresh()
        out_dir = cfg.OUTPUTS_DIR / "m9"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "a.csv").write_text("x", encoding="utf-8")
        (cfg.LOGS_DIR / "m9.log").write_text("y", encoding="utf-8")

        out_resp = asyncio_run(fr.clear_all_outputs())
        log_resp = asyncio_run(fr.clear_all_logs())

        self.assertTrue(out_resp["success"])
        self.assertTrue(log_resp["success"])
        self.assertFalse(out_dir.exists())
        self.assertFalse((cfg.LOGS_DIR / "m9.log").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
