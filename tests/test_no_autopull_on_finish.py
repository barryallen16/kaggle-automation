"""No background download when a run finishes (server crash guard)."""

import asyncio
import os
import shutil
import sys
import tempfile
import unittest
from datetime import UTC, datetime

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TEST_DIR)

DATA_TMP = tempfile.mkdtemp(prefix="no_autopull_")
os.environ["AUTOMATION_DATA_DIR"] = DATA_TMP


def tearDownModule():
    shutil.rmtree(DATA_TMP, ignore_errors=True)
    os.environ.pop("AUTOMATION_DATA_DIR", None)


class TestNoAutoPullOnFinish(unittest.TestCase):
    def test_complete_marks_done_without_downloading(self):
        from services import session_monitor as sm

        run = {
            "id": "r1",
            "account_username": "acc",
            "kernel_ref": "acc/slug",
            "status": "running",
            "start_time": datetime.now(UTC).isoformat(),
            "is_trial": 0,
            "accelerator": "none",
            "telegram_notified_start": 1,
            "telegram_notified_end": 0,
        }
        calls = {"download": 0, "status": [], "notify": 0}

        async def fake_status(*a, **k):
            return {"status": "complete", "raw": "complete"}

        async def fake_download(*a, **k):
            calls["download"] += 1
            return (None, None, [])

        async def fake_notify(*a, **k):
            calls["notify"] += 1

        sm.KaggleService.get_kernel_status = staticmethod(fake_status)
        sm.KaggleService.download_latest_outputs = staticmethod(fake_download)
        sm.TelegramService.notify = staticmethod(fake_notify)
        sm.update_run_status = lambda **k: calls["status"].append(k)
        sm.update_run_telegram_flag = lambda *a, **k: None

        asyncio.run(sm.SessionMonitor._check_single_run(run, datetime.now(UTC)))

        self.assertEqual(calls["download"], 0)
        self.assertTrue(any(s.get("status") == "complete" for s in calls["status"]))
        self.assertEqual(calls["notify"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
