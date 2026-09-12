"""Regression: files explorer must never crash on nameless rows.

`renderFilesTable` did `f.name.split('.')` unguarded, so one remote row
without `name`/`fileName` (non-CSV CLI output, renamed header) crashed the
whole table with `Cannot read properties of undefined (reading 'split')`
even after a successful 66-file pull. Backend now normalizes + drops
nameless rows; versioned 403 stops probing early; hard-drive icon renders.
"""

import asyncio
import os
import shutil
import sys
import tempfile
import unittest

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TEST_DIR)

DATA_TMP = tempfile.mkdtemp(prefix="split_guard_")
os.environ["AUTOMATION_DATA_DIR"] = DATA_TMP


def tearDownModule():
    shutil.rmtree(DATA_TMP, ignore_errors=True)
    os.environ.pop("AUTOMATION_DATA_DIR", None)


def _ks():
    import importlib

    import services.kaggle_service as ks

    return importlib.reload(ks)


class FakeProc:
    def __init__(self, out: bytes):
        self._out = out

    async def communicate(self):
        return self._out, b""


def _run(coro):
    return asyncio.run(coro)


class TestListOutputFilesNormalization(unittest.TestCase):
    def _list_with_stdout(self, out: bytes):
        ks = _ks()
        real = asyncio.create_subprocess_exec

        async def fake(*a, **k):
            return FakeProc(out)

        asyncio.create_subprocess_exec = fake
        try:
            return _run(ks.KaggleService.list_output_files("acc", "acc/slug"))
        finally:
            asyncio.create_subprocess_exec = real

    def test_mixed_rows_drop_nameless(self):
        out = (
            b"name,size,creationDate\n"
            b"a.jsonl,10,2026-09-16T00:00:00Z\n"
            b",10,2026-09-16T00:00:00Z\n"
            b"  ,5,2026-09-16T00:00:00Z\n"
            b"b.csv,20,2026-09-16T00:00:00Z\n"
        )
        files = self._list_with_stdout(out)
        self.assertEqual([f["name"] for f in files], ["a.jsonl", "b.csv"])

    def test_non_csv_output_returns_empty(self):
        for blob in (b"No files found\n", b"Error: something broke\n", b"\n", b""):
            self.assertEqual(self._list_with_stdout(blob), [])


class TestVersionedPermissionShortCircuit(unittest.TestCase):
    def test_403_stops_after_first_label(self):
        import app.services.kaggle_versioned_output as helper

        calls = []

        def handler(request):
            body = b"{}"
            try:
                import json

                body = request.content or b"{}"
                label = json.loads(body).get("versionLabel", "")
            except Exception:
                label = ""
            calls.append(label)
            if request.url.path.endswith("DownloadKernelOutput"):
                return __import__("httpx").Response(200, json={})
            return __import__("httpx").Response(
                403,
                json={
                    "error": {
                        "code": 403,
                        "message": "Permission 'kernels.get' was denied",
                    }
                },
            )

        import httpx

        real_client = httpx.Client
        out_dir = tempfile.mkdtemp(prefix="perm403_")

        def fake_client(*a, **kw):
            kw["transport"] = httpx.MockTransport(handler)
            return real_client(*a, **kw)

        helper.httpx.Client = staticmethod(fake_client)
        try:
            saved, notes = helper.fetch_version_output("o", "s", 1, out_dir)
        finally:
            helper.httpx.Client = real_client
        self.assertEqual(saved, [])
        # Only the first label attempted, not all six spellings.
        self.assertEqual(len([c for c in calls if c]), 1)
        self.assertTrue(any("plain latest pull" in n for n in notes))


class TestPixelIcon(unittest.TestCase):
    def test_hard_drive_renders_path(self):
        repo = os.path.dirname(TEST_DIR)
        p = os.path.join(repo, "app", "static", "js", "pixel-icons.js")
        with open(p, encoding="utf-8") as f:
            txt = f.read()
        i = txt.find('"hard-drive"')
        self.assertGreater(i, 0)
        snippet = txt[i : i + 400]
        self.assertIn("<path", snippet)


if __name__ == "__main__":
    unittest.main(verbosity=2)
