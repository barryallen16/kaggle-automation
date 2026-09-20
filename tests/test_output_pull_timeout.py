"""Pre-stop output snapshot must time out instead of wedging Stop forever.

_stop_kernel_impl pulls latest outputs BEFORE pushing the stop stub. A kernel
that published gigabytes (models in /kaggle/working instead of scratch)
stalls that pull indefinitely: no timeout meant the stop request, the ops
tracker entry, and the Stop button hung with it. download_outputs now raises
after OUTPUT_PULL_TIMEOUT_SECONDS so the stop pipeline continues.
"""

import asyncio
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TEST_DIR)

DATA_TMP = None


def setUpModule():
    global DATA_TMP
    DATA_TMP = tempfile.mkdtemp(prefix="output_pull_timeout_")
    os.environ["AUTOMATION_DATA_DIR"] = DATA_TMP


def tearDownModule():
    if DATA_TMP and os.path.isdir(DATA_TMP):
        shutil.rmtree(DATA_TMP, ignore_errors=True)
    os.environ.pop("AUTOMATION_DATA_DIR", None)


def _fresh():
    import importlib

    import config as cfg

    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(str(cfg.DB_PATH) + suffix)
        except OSError:
            pass
    importlib.reload(cfg)
    ks = importlib.reload(__import__("services.kaggle_service", fromlist=["x"]))
    return ks.KaggleService


class _HungProc:
    def __init__(self):
        self.killed = False

    async def communicate(self):
        await asyncio.sleep(30)
        return (b"", b"")

    def kill(self):
        self.killed = True


class TestOutputPullTimeout(unittest.TestCase):
    def test_hung_pull_raises_and_kills(self):
        svc = _fresh()
        import services.kaggle_service as ksmod

        ksmod.OUTPUT_PULL_TIMEOUT_SECONDS = 1
        hung = _HungProc()

        async def fake_exec(*a, **k):
            return hung

        async def go():
            with (
                mock.patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
                self.assertRaises(RuntimeError) as ctx,
            ):
                await svc.download_outputs("acc", "acc/slug", "run1")
            self.assertIn("timed out", str(ctx.exception))

        asyncio.run(go())
        self.assertTrue(hung.killed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
