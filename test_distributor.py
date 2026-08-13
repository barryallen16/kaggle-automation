"""Hermetic tests for the multi-session distributed runner.

Covers: runner expansion (2 sessions/account), silent reduction when slots are
busy, full rejection, CPU-run filter, odd-remainder splits, atomic conflict
rejection, and the stop-workload endpoint. Uses a stub CLI + temp data dir so
nothing touches production or real Kaggle.
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


def _make_stub_cli() -> str:
    """Writes a zero-exit stub CLI executable for the host OS.

    Windows cannot exec '#!/bin/bash' scripts via CreateProcess (WinError 193),
    so a .bat stub is used there; POSIX keeps the shell script.
    """
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
    DATA_TMP = tempfile.mkdtemp(prefix="dist_harness_")
    os.environ["AUTOMATION_DATA_DIR"] = DATA_TMP
    os.environ["INTER_PUSH_STAGGER_SECONDS"] = "0"

    import app.services.kaggle_service as ks
    ks.get_kaggle_cli_path = lambda: _make_stub_cli()

    # Neutralize background log followers: with a silent stub CLI they would
    # treat every poll as 'unknown' and reconnect forever during tests.
    async def _noop_stream(*a, **k):
        return
    ks.KaggleService.start_background_log_stream = staticmethod(_noop_stream)


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
    import app.services.kaggle_service as ks
    # Wipe DB files so row counts don't leak across tests
    db_file = cfg.DB_PATH
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(str(db_file) + suffix)
        except OSError:
            pass
    importlib.reload(cfg)
    importlib.reload(db)
    importlib.reload(ks)
    ks.get_kaggle_cli_path = lambda: _make_stub_cli()
    async def _noop_stream(*a, **k):
        return
    ks.KaggleService.start_background_log_stream = staticmethod(_noop_stream)
    db.init_db()
    return db


def seed_run(db, run_id, account, status="running", accelerator="nvidia-tesla-t4-x2",
             kernel_ref="acc/some-kernel", workload_id=None):
    db.create_run_record({
        "id": run_id,
        "account_username": account,
        "kernel_slug": kernel_ref.split("/", 1)[1],
        "kernel_ref": kernel_ref,
        "title": "Seeded",
        "code_file": "x.py",
        "accelerator": accelerator,
        "enable_internet": 1,
        "is_trial": 0,
        "timeout_seconds": 300,
        "status": status,
        "status_message": "",
        "start_time": "2026-08-23T00:00:00+00:00",
        "kaggle_url": "https://www.kaggle.com/code/" + kernel_ref,
        "workload_id": workload_id,
        "shard_index": None,
        "total_shards": None,
        "log_file": "",
    })


CODE = 'print("shard")'


class TestMultiSessionDistributor(unittest.TestCase):
    def _dist(self):
        import app.services.workload_distributor as wd
        return wd.WorkloadDistributor, wd

    def test_1_expansion_two_runners_one_account(self):
        """sessions=2 on an idle account -> 2 runners, split [0,5)/[5,10]."""
        db = _fresh()
        WD, _ = self._dist()
        res = asyncio_run(WD.distribute_and_launch(
            base_title="Timing", code_content=CODE, filename="main.py",
            accounts=["accA"], total_items=10, accelerator="nvidia-tesla-t4-x2",
            sessions_per_account=2))
        self.assertTrue(res["success"], res)
        self.assertEqual(res["total_shards"], 2)
        self.assertEqual(res["status"], "dispatched")
        ranges = sorted(tuple(s["range"]) for s in res["shards"])
        self.assertEqual(ranges[0][0], 0)
        # remainder math over R=2 for 10 items -> 5/5
        self.assertEqual([tuple(s["range"]) for s in sorted(res["shards"], key=lambda x: x["shard_index"])],
                         [(0, 5), (5, 10)])
        wls = db.get_all_workloads()
        self.assertEqual(len(wls), 1)
        meta = wls[0]["accounts_used"]
        self.assertEqual(meta["runners"], [{"account": "accA", "shard_index": 0},
                                           {"account": "accA", "shard_index": 1}])

    def test_2_silent_reduction_when_busy(self):
        """1 active GPU run + sessions=2 -> silently reduced to 1 runner."""
        db = _fresh()
        seed_run(db, "seed1", "accA", status="running",
                 kernel_ref="accA/unrelated-kernel")
        WD, _ = self._dist()
        res = asyncio_run(WD.distribute_and_launch(
            base_title="Timing", code_content=CODE, filename="main.py",
            accounts=["accA"], total_items=10, accelerator="nvidia-tesla-t4-x2",
            sessions_per_account=2))
        self.assertTrue(res["success"])
        self.assertEqual(res["total_shards"], 1)  # reduced from 2
        plan = res["runner_plan"][0]
        self.assertEqual((plan["requested"], plan["busy"], plan["slots"]), (2, 1, 1))

    def test_3_full_rejection_no_side_effects(self):
        """2 active GPU runs -> zero free slots; NO workload row is created."""
        db = _fresh()
        seed_run(db, "s1", "accA", kernel_ref="accA/k1")
        seed_run(db, "s2", "accA", kernel_ref="accA/k2")
        WD, _ = self._dist()
        res = asyncio_run(WD.distribute_and_launch(
            base_title="Timing", code_content=CODE, filename="main.py",
            accounts=["accA"], total_items=10, accelerator="nvidia-tesla-t4-x2",
            sessions_per_account=2))
        self.assertFalse(res["success"])
        self.assertIn("No free GPU session slots", res["error"])
        self.assertEqual(len(db.get_all_workloads()), 0)  # atomic: nothing created

    def test_4_cpu_runs_do_not_consume_gpu_slots(self):
        """Active CPU runs must not block GPU session slots."""
        db = _fresh()
        seed_run(db, "c1", "accA", status="running", accelerator="none",
                 kernel_ref="accA/cpu-job-1")
        seed_run(db, "c2", "accA", status="running", accelerator="none",
                 kernel_ref="accA/cpu-job-2")
        WD, _ = self._dist()
        res = asyncio_run(WD.distribute_and_launch(
            base_title="Timing", code_content=CODE, filename="main.py",
            accounts=["accA"], total_items=10, accelerator="nvidia-tesla-t4-x2",
            sessions_per_account=2))
        self.assertTrue(res["success"])
        self.assertEqual(res["total_shards"], 2)  # CPU runs ignored

    def test_5_remainder_split_across_runners(self):
        """7 items over 2 runners -> 4/3 with contiguous non-overlapping ranges."""
        db = _fresh()
        WD, _ = self._dist()
        res = asyncio_run(WD.distribute_and_launch(
            base_title="Rem", code_content=CODE, filename="main.py",
            accounts=["accA"], total_items=7, accelerator="nvidia-tesla-t4-x2",
            sessions_per_account=2))
        self.assertTrue(res["success"])
        ordered = sorted(res["shards"], key=lambda s: s["shard_index"])
        self.assertEqual([(s["range"], s["count"]) for s in ordered],
                         [([0, 4], 4), ([4, 7], 3)])

    def test_6_atomic_conflict_rejection(self):
        """If any planned ref collides with an active run, reject BEFORE creating the workload."""
        db = _fresh()
        WD, _ = self._dist()
        from app.services.kaggle_service import KaggleService
        # Second account holds an active run whose ref matches the runner we'd
        # dispatch there ([Shard 2/2], since sessions=1 -> one runner each).
        colliding_ref = f"accB/{KaggleService.sanitize_slug('Timing [Shard 2/2]')}"
        seed_run(db, "blocker", "accB", kernel_ref=colliding_ref)
        res = asyncio_run(WD.distribute_and_launch(
            base_title="Timing", code_content=CODE, filename="main.py",
            accounts=["accA", "accB"], total_items=10,
            accelerator="nvidia-tesla-t4-x2",
            sessions_per_account=1))
        self.assertFalse(res["success"])
        self.assertEqual(res.get("status"), "conflict")
        self.assertIn(colliding_ref, res["error"])
        self.assertEqual(len(db.get_all_workloads()), 0)  # no side effects

    def test_7_stop_workload_endpoint(self):
        """Stop endpoint kills all active shards and marks the workload stopped."""
        db = _fresh()
        WD, _ = self._dist()
        res = asyncio_run(WD.distribute_and_launch(
            base_title="StopMe", code_content=CODE, filename="main.py",
            accounts=["accA"], total_items=10, accelerator="nvidia-tesla-t4-x2",
            sessions_per_account=2))
        wid = res["workload_id"]

        # Simulate the shards being actively running under this workload
        runs = db.get_active_runs()
        targets = [r for r in runs if r.get("workload_id") == wid]
        self.assertEqual(len(targets), 2)
        for r in targets:
            db.update_run_status(r["id"], "running")

        from app.routers.distributed import stop_workload
        out = asyncio_run(stop_workload(wid))
        self.assertTrue(out["success"])
        self.assertEqual(out["message"], "Stopped 2/2 shards.")
        for r in targets:
            self.assertEqual(db.get_run_by_id(r["id"])["status"], "stopped")
        self.assertEqual(db.get_all_workloads()[0]["status"], "stopped")

    def test_8_stop_workload_with_no_active_shards_404(self):
        db = _fresh()
        from app.routers.distributed import stop_workload
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as cm:
            asyncio_run(stop_workload("workload_does_not_exist"))
        self.assertEqual(cm.exception.status_code, 404)

    def test_9_per_account_sessions_map(self):
        """sessions_per_account as {accA:1, accB:2} expands 3 runners total."""
        db = _fresh()
        WD, _ = self._dist()
        res = asyncio_run(WD.distribute_and_launch(
            base_title="MapTest", code_content=CODE, filename="main.py",
            accounts=["accA", "accB"], total_items=10,
            accelerator="nvidia-tesla-t4-x2",
            sessions_per_account={"accA": 1, "accB": 2}))
        self.assertTrue(res["success"], res)
        self.assertEqual(res["total_shards"], 3)
        plan = {p["account"]: p for p in res["runner_plan"]}
        self.assertEqual(plan["accA"]["requested"], 1)
        self.assertEqual(plan["accA"]["slots"], 1)
        self.assertEqual(plan["accB"]["requested"], 2)
        self.assertEqual(plan["accB"]["slots"], 2)
        # workload metadata stores the per-account map
        meta = db.get_all_workloads()[0]["accounts_used"]
        self.assertEqual(meta["sessions_per_account"], {"accA": 1, "accB": 2})

    def test_10_per_account_map_clamped_and_missing_defaults(self):
        """Out-of-range values clamp to 1..2; accounts absent from map default to 2."""
        db = _fresh()
        WD, _ = self._dist()
        res = asyncio_run(WD.distribute_and_launch(
            base_title="ClampTest", code_content=CODE, filename="main.py",
            accounts=["accA", "accB"], total_items=12,
            accelerator="nvidia-tesla-t4-x2",
            sessions_per_account={"accA": 99}))  # accB missing -> default 2
        self.assertTrue(res["success"])
        plan = {p["account"]: p for p in res["runner_plan"]}
        self.assertEqual(plan["accA"]["requested"], 2)   # clamped from 99
        self.assertEqual(plan["accB"]["requested"], 2)   # defaulted
        self.assertEqual(res["total_shards"], 4)

    def test_11_manual_shards_distribution(self):
        """manual_shards with custom ranges and accounts launches exact partitions."""
        db = _fresh()
        WD, _ = self._dist()
        manual_shards = [
            {"account": "accA", "start_index": 0, "end_index": 100},
            {"account": "accB", "start_index": 100, "end_index": 500}
        ]
        res = asyncio_run(WD.distribute_and_launch(
            base_title="ManualShardsTest",
            code_content=CODE,
            filename="main.py",
            accounts=["accA", "accB"],
            total_items=500,
            accelerator="none",
            manual_shards=manual_shards
        ))
        self.assertTrue(res["success"], res)
        self.assertEqual(res["total_shards"], 2)
        self.assertEqual(res["total_units"], 500)
        ordered = sorted(res["shards"], key=lambda s: s["shard_index"])
        self.assertEqual(ordered[0]["range"], [0, 100])
        self.assertEqual(ordered[0]["account"], "accA")
        self.assertEqual(ordered[1]["range"], [100, 500])
        self.assertEqual(ordered[1]["account"], "accB")

        wls = db.get_all_workloads()
        self.assertEqual(len(wls), 1)
        self.assertEqual(wls[0]["workload_type"], "manual_range")
        self.assertEqual(wls[0]["total_units"], 500)

    def test_12_manual_shards_validation_invalids(self):
        """Rejects inverted range start > end or missing accounts."""
        db = _fresh()
        WD, _ = self._dist()
        # Inverted range
        res = asyncio_run(WD.distribute_and_launch(
            base_title="InvalidRange",
            code_content=CODE,
            filename="main.py",
            accounts=["accA"],
            total_items=100,
            manual_shards=[{"account": "accA", "start_index": 200, "end_index": 50}]
        ))
        self.assertFalse(res["success"])
        self.assertIn("greater than end index", res["error"])

        # Missing account
        res2 = asyncio_run(WD.distribute_and_launch(
            base_title="MissingAccount",
            code_content=CODE,
            filename="main.py",
            accounts=[],
            total_items=100,
            manual_shards=[{"account": "", "start_index": 0, "end_index": 50}]
        ))
        self.assertFalse(res2["success"])
        self.assertIn("missing a target Kaggle account", res2["error"])

    def test_13_accounts_descending_quota_sort(self):
        """list_accounts returns accounts sorted descending by remaining quota."""
        db = _fresh()
        from app.routers.accounts import list_accounts
        # Seed 3 accounts with varying GPU quotas
        # acc_low: 5h left (used 25/30)
        db.save_account("1", "acc_low", "key1", {"gpu": {"limit": 30, "used": 25}, "tpu": {"limit": 20, "used": 0}})
        # acc_high: 28h left (used 2/30)
        db.save_account("2", "acc_high", "key2", {"gpu": {"limit": 30, "used": 2}, "tpu": {"limit": 20, "used": 0}})
        # acc_mid: 15h left (used 15/30)
        db.save_account("3", "acc_mid", "key3", {"gpu": {"limit": 30, "used": 15}, "tpu": {"limit": 20, "used": 0}})

        resp = asyncio_run(list_accounts())
        self.assertTrue(resp["success"])
        names = [a["username"] for a in resp["accounts"]]
        self.assertEqual(names, ["acc_high", "acc_mid", "acc_low"])


def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)


if __name__ == "__main__":
    unittest.main(verbosity=2)
