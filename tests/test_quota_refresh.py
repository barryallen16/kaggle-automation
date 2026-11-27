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
    DATA_TMP = tempfile.mkdtemp(prefix="quota_harness_")
    os.environ["AUTOMATION_DATA_DIR"] = DATA_TMP


def tearDownModule():
    if DATA_TMP and os.path.isdir(DATA_TMP):
        shutil.rmtree(DATA_TMP, ignore_errors=True)
    os.environ.pop("AUTOMATION_DATA_DIR", None)


def _fresh_am():
    import importlib

    import config as cfg
    import database as db
    db_file = cfg.DB_PATH
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(str(db_file) + suffix)
        except OSError:
            pass
    importlib.reload(cfg)
    importlib.reload(db)
    import services.account_manager as am_mod
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
            _cfg, db, AM = _fresh_am()
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

            self.assertEqual(state["max_active"], AM.QUOTA_CONCURRENCY_LIMIT)
        finally:
            os.environ.pop("QUOTA_REFRESH_CONCURRENCY", None)

    def test_2_single_flight_joins_inflight_run(self):
        _cfg, db, AM = _fresh_am()
        _seed_accounts(db, 4)

        batches = {"calls": 0, "active": 0, "peak": 0}

        async def fake_lookup(username):
            batches["calls"] += 1
            batches["active"] += 1
            batches["peak"] = max(batches["peak"], batches["active"])
            try:
                await asyncio.sleep(0.15)
                return {"gpu": {"used": 0, "limit": 30}}
            finally:
                batches["active"] -= 1

        AM._fetch_quota_unthrottled = staticmethod(fake_lookup)

        async def scenario():
            t1 = asyncio.create_task(AM.refresh_all_quotas())
            await asyncio.sleep(0.03)
            t2 = asyncio.create_task(AM.refresh_all_quotas())
            return await asyncio.gather(t1, t2)

        res1, res2 = asyncio.run(scenario())
        self.assertEqual(batches["calls"], 4)
        self.assertLessEqual(batches["peak"], AM.QUOTA_CONCURRENCY_LIMIT)
        self.assertEqual(len(res1), 4)
        self.assertEqual(len(res2), 4)

    def test_3_timeout_returns_graceful_error(self):
        _cfg, _db, AM = _fresh_am()
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
        self.assertIn("gpu", result)

    def test_4_lock_and_semaphore_isolated_across_event_loops(self):
        _cfg, _db, AM = _fresh_am()

        async def use_lock_only():
            async with AM._get_lock():
                return AM._lock_loop_id

        async def use_both():
            async with AM._get_lock(), AM._get_quota_semaphore():
                return AM._lock_loop_id, AM._quota_semaphore_loop_id

        loop1_lock_id, loop1_sem_id = asyncio.run(use_both())
        loop2_lock_id = asyncio.run(use_lock_only())

        self.assertEqual(loop1_lock_id, loop1_sem_id)
        self.assertEqual(AM._quota_semaphore_loop_id, loop1_sem_id)
        self.assertEqual(AM._lock_loop_id, loop2_lock_id)
        self.assertIsNotNone(loop2_lock_id)



import asyncio

if __name__ == "__main__":
    unittest.main(verbosity=2)
