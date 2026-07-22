"""Tests for the cat-style cross-run file merge endpoint.

The merge must be raw byte concatenation in cart order - exactly
`cat shard/* > merged.jsonl` - with no parsing, dedupe or re-encoding,
and must fall back to pulling a run's outputs when a file is local-missing.
"""

import os
import sys
import shutil
import tempfile
import unittest

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TEST_DIR)

DATA_TMP = tempfile.mkdtemp(prefix="merge_harness_")
os.environ["AUTOMATION_DATA_DIR"] = DATA_TMP


def setUpModule():
    pass


def tearDownModule():
    if os.path.isdir(DATA_TMP):
        shutil.rmtree(DATA_TMP, ignore_errors=True)
    os.environ.pop("AUTOMATION_DATA_DIR", None)


def _fresh():
    """Reload config/database/service/files modules bound to the temp dir."""
    global cfg, db, files_router
    import importlib
    import app.config as _cfg
    import app.database as _db
    import app.services.kaggle_service as _ks
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(str(_cfg.DB_PATH) + suffix)
        except OSError:
            pass
    cfg = importlib.reload(_cfg)
    db = importlib.reload(_db)
    ks = importlib.reload(_ks)
    # Stub CLI: pushes/pulls would fail fast if ever invoked
    ks.get_kaggle_cli_path = lambda: os.path.join(DATA_TMP, "no_such_kaggle_cli")
    import app.routers.files as _files
    files_router = importlib.reload(_files)
    db.init_db()
    return cfg, db, files_router, __import__("app.services.kaggle_service", fromlist=["x"]).KaggleService


def _seed_run(db, run_id):
    db.create_run_record({
        "id": run_id,
        "account_username": f"acc_{run_id}",
        "kernel_slug": f"slug-{run_id}",
        "kernel_ref": f"acc_{run_id}/slug-{run_id}",
        "title": f"T-{run_id}",
        "code_file": "",
        "accelerator": "none",
        "enable_internet": 1,
        "is_trial": 0,
        "timeout_seconds": 300,
        "status": "complete",
        "status_message": "",
        "start_time": "2026-01-01T00:00:00+00:00",
        "kaggle_url": "https://www.kaggle.com/code/x/y",
        "workload_id": None,
        "shard_index": None,
        "total_shards": None,
        "log_file": "",
    })


class TestCatMerge(unittest.TestCase):
    def _setup(self):
        return _fresh()

    def test_1_jsonl_concat_in_cart_order(self):
        cfg, db, fr, ks = self._setup()
        _seed_run(db, "m1")
        _seed_run(db, "m2")

        out1 = cfg.OUTPUTS_DIR / "m1"
        out2 = cfg.OUTPUTS_DIR / "m2"
        out1.mkdir(parents=True, exist_ok=True)
        out2.mkdir(parents=True, exist_ok=True)
        a_bytes = b'{"id": 0, "s": "a"}\n{"id": 1, "s": "b"}\n'
        b_bytes = b'{"id": 1, "s": "DUP-KEPT-CAT-DOES-NOT-DEDUPE"}\n{"id": 2}\n'
        (out1 / "shard_0.jsonl").write_bytes(a_bytes)
        (out2 / "shard_1.jsonl").write_bytes(b_bytes)

        from pydantic import BaseModel  # noqa: F401  (import sanity)
        req = fr.MergeRequest(items=[
            fr.MergeItem(run_id="m1", filename="shard_0.jsonl"),
            fr.MergeItem(run_id="m2", filename="shard_1.jsonl"),
        ])
        resp = asyncio_run(fr.merge_selected_files(req))

        self.assertEqual(resp.filename, "merged.jsonl")
        merged = open(resp.path, "rb").read()
        # Pure cat: exact bytes, exact order, duplicate ids preserved
        self.assertEqual(merged, a_bytes + b_bytes)

    def test_2_extension_follows_majority_and_binary_intact(self):
        cfg, db, fr, ks = self._setup()
        _seed_run(db, "m3")
        out = cfg.OUTPUTS_DIR / "m3"
        out.mkdir(parents=True, exist_ok=True)
        (out / "part0.jsonl").write_bytes(b'{"x":1}\n')
        (out / "part1.jsonl").write_bytes(b'{"x":2}\n')
        bin_marker = b"\x00\x01\x02BINARYMARKER\xff"
        (out / "blob.bin").write_bytes(bin_marker)

        req = fr.MergeRequest(items=[
            fr.MergeItem(run_id="m3", filename="part0.jsonl"),
            fr.MergeItem(run_id="m3", filename="blob.bin"),
            fr.MergeItem(run_id="m3", filename="part1.jsonl"),
        ])
        resp = asyncio_run(fr.merge_selected_files(req))
        self.assertEqual(resp.filename, "merged.jsonl")  # majority suffix wins
        merged = open(resp.path, "rb").read()
        self.assertTrue(merged.startswith(b'{"x":1}\n'))
        self.assertIn(bin_marker, merged)
        self.assertTrue(merged.endswith(b'{"x":2}\n'))

    def test_3_missing_file_attempts_autopull_then_502(self):
        cfg, db, fr, ks = self._setup()
        ks = __import__("app.services.kaggle_service", fromlist=["x"]).KaggleService
        _seed_run(db, "m4")
        (cfg.OUTPUTS_DIR / "m4").mkdir(parents=True, exist_ok=True)

        # No version lookup possible in tests - helper would hit the network
        async def no_version(account, ref):
            return None

        ks.get_kernel_current_version = staticmethod(no_version)

        req = fr.MergeRequest(items=[fr.MergeItem(run_id="m4", filename="nope.jsonl")])
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as cm:
            asyncio_run(fr.merge_selected_files(req))
        self.assertEqual(cm.exception.status_code, 502)

    def test_4_empty_cart_rejected(self):
        cfg, db, fr, ks = self._setup()
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as cm:
            asyncio_run(fr.merge_selected_files(fr.MergeRequest(items=[])))
        self.assertEqual(cm.exception.status_code, 400)


def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)


if __name__ == "__main__":
    unittest.main(verbosity=2)
