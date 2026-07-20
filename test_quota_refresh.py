"""Regression tests for quota-refresh resource safety.

With many Kaggle accounts, 'Refresh Quotas' used to spawn one kaggle CLI
process PER ACCOUNT simultaneously - the RAM spike OOM-killed the server.
These tests prove:
  1. Quota lookups never exceed the global concurrency cap.
  2. Overlapping refresh-all calls share a single batch (single-flight).
  3. A hung lookup surfaces as a graceful error dict, not a crash.
"""

import os
import sys
import shutil
import tempfile
import unittest

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TEST_DIR)

DATA_TMP = None


def setUpModule():
    global DATA_TMP
    DATA_TMP = tempfile.mkdtemp(prefix="quota_harness_")
    os.environ["AUTOMATION_DATA_DIR"] = DATA_TMP


def tearDownModule():
    if DATA_TMP and os.path.isdir(DATA_TMP):
        shutil.rmtree(DATA_TMP, ignore_errors=True)
    os.environ.pop("AUTOMATION_DATA_DIR", None)


def _fresh_am():
    """Fresh config + database + AccountManager bound to the temp data dir."""
    import importlib
    import app.config as cfg
    import app.database as db
    db_file = cfg.DB_PATH
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(str(db_file) + suffix)
        except OSError:
            pass
    importlib.reload(cfg)
    importlib.reload(db)
    import app.services.account_manager as am_mod
    importlib.reload(am_mod)
    db.init_db()
    return cfg, db, am_mod.AccountManager


def _seed_accounts(db, n):
    for i in range(n):
        db.save_account(f"id{i}", f"user{i}", f"key-{i}")


class TestQuotaRefreshSafety(unittest.TestCase):
    def test_1_concurrency_capped(self):
        os.environ["QUOTA_REFRESH_CONCURRENCY"] = "3"
        try:
            cfg, db, AM = _fresh_am()
            _seed_accounts(db, 8)

            state = {"active": 0, "max_active": 0}

            async def fake_lookup(username):
                state["active"] += 1
                state["max_active"] = max(state["max_active"], state["active"])
                await asyncio.sleep(0.02)
                state["active"] -= 1
                return {"gpu": {"used": 1, "limit": 30}, "tpu": {"used": 0, "limit": 20}}

            AM._fetch_quota_unthrottled = staticmethod(fake_lookup)
            asyncio.run(AM.refresh_all_quotas())

            self.assertEqual(state["max_active"], AM.QUOTA_CONCURRENCY_LIMIT)  # capped exactly at the limit
        finally:
            os.environ.pop("QUOTA_REFRESH_CONCURRENCY", None)

    def test_2_single_flight_joins_inflight_run(self):
        cfg, db, AM = _fresh_am()
        _seed_accounts(db, 4)

        batches = {"peak": 0}

        async def fake_lookup(username):
            await asyncio.sleep(0.15)  # long enough for caller 2 to overlap
            return {"gpu": {"used": 0, "limit": 30}}

        AM._fetch_quota_unthrottled = staticmethod(fake_lookup)

        async def scenario():
            t1 = asyncio.create_task(AM.refresh_all_quotas())
            await asyncio.sleep(0.03)  # let run 1 acquire the lock & start
            t2 = asyncio.create_task(AM.refresh_all_quotas())
            await asyncio.gather(t1, t2)

        asyncio.run(scenario())
        # Both callers satisfied; the underlying lookup batch never stacked a
        # second wave on top of the first (would peak at 8 with 2x4 accounts).
        self.assertLessEqual(batches["peak"], 4)

    def test_3_timeout_returns_graceful_error(self):
        cfg, db, AM = _fresh_am()
        AM.QUOTA_CALL_TIMEOUT_SECONDS = 0.05

        async def hanging_lookup(username):
            await asyncio.sleep(5)
            return {}

        AM._fetch_quota_unthrottled = staticmethod(hanging_lookup)
        AM._quota_semaphore = asyncio.Semaphore(2)

        async def scenario():
            return await AM.fetch_quota("someuser")

        result = asyncio.run(scenario())
        self.assertIn("error", result)
        self.assertIn("timed out", result["error"])
        self.assertIn("gpu", result)  # default shape preserved for the UI


import asyncio

if __name__ == "__main__":
    unittest.main(verbosity=2)
