import os
import shutil
import sys
import tempfile
import unittest

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TEST_DIR)

DATA_TMP = None


_STUB_CLI = None


def _make_stub_cli() -> str:
    global _STUB_CLI
    if _STUB_CLI and os.path.exists(_STUB_CLI):
        return _STUB_CLI
    if os.name == "nt":
        stub = os.path.join(DATA_TMP, "fake_kaggle.bat")
        with open(stub, "w") as f:
            f.write("@echo off\r\nexit /b 0\r\n")
    else:
        stub = os.path.join(DATA_TMP, "fake_kaggle")
        with open(stub, "w") as f:
            f.write("#!/bin/bash\nexit 0\n")
        os.chmod(stub, 0o755)
    _STUB_CLI = stub
    return stub


def setUpModule():
    global DATA_TMP
    DATA_TMP = tempfile.mkdtemp(prefix="dist_harness_")
    os.environ["AUTOMATION_DATA_DIR"] = DATA_TMP
    os.environ["INTER_PUSH_STAGGER_SECONDS"] = "0"

    import services.kaggle_service as ks

    ks.get_kaggle_cli_path = lambda: _make_stub_cli()

    async def _noop_stream(*a, **k):
        return

    ks.KaggleService.start_background_log_stream = staticmethod(_noop_stream)


def tearDownModule():
    if DATA_TMP and os.path.isdir(DATA_TMP):
        shutil.rmtree(DATA_TMP, ignore_errors=True)
    os.environ.pop("AUTOMATION_DATA_DIR", None)
    os.environ.pop("INTER_PUSH_STAGGER_SECONDS", None)


def _fresh():
    import importlib

    import config as cfg
    import database as db
    import services.kaggle_service as ks

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


def seed_run(
    db,
    run_id,
    account,
    status="running",
    accelerator="nvidia-tesla-t4-x2",
    kernel_ref="acc/some-kernel",
    workload_id=None,
):
    db.create_run_record(
        {
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
        }
    )


CODE = 'print("shard")'


class TestMultiSessionDistributor(unittest.TestCase):
    def _dist(self):
        import services.workload_distributor as wd

        return wd.WorkloadDistributor, wd

    def test_1_expansion_two_runners_one_account(self):
        db = _fresh()
        WD, _ = self._dist()
        res = asyncio_run(
            WD.distribute_and_launch(
                base_title="Timing",
                code_content=CODE,
                filename="main.py",
                accounts=["accA"],
                total_items=10,
                accelerator="nvidia-tesla-t4-x2",
                sessions_per_account=2,
            )
        )
        self.assertTrue(res["success"], res)
        self.assertEqual(res["total_shards"], 2)
        self.assertEqual(res["status"], "dispatched")
        ranges = sorted(tuple(s["range"]) for s in res["shards"])
        self.assertEqual(ranges[0][0], 0)
        self.assertEqual(
            [
                tuple(s["range"])
                for s in sorted(res["shards"], key=lambda x: x["shard_index"])
            ],
            [(0, 5), (5, 10)],
        )
        wls = db.get_all_workloads()
        self.assertEqual(len(wls), 1)
        meta = wls[0]["accounts_used"]
        self.assertEqual(
            meta["runners"],
            [
                {"account": "accA", "shard_index": 0},
                {"account": "accA", "shard_index": 1},
            ],
        )

    def test_2_silent_reduction_when_busy(self):
        db = _fresh()
        seed_run(
            db, "seed1", "accA", status="running", kernel_ref="accA/unrelated-kernel"
        )
        WD, _ = self._dist()
        res = asyncio_run(
            WD.distribute_and_launch(
                base_title="Timing",
                code_content=CODE,
                filename="main.py",
                accounts=["accA"],
                total_items=10,
                accelerator="nvidia-tesla-t4-x2",
                sessions_per_account=2,
            )
        )
        self.assertTrue(res["success"])
        self.assertEqual(res["total_shards"], 1)
        plan = res["runner_plan"][0]
        self.assertEqual((plan["requested"], plan["busy"], plan["slots"]), (2, 1, 1))

    def test_3_full_rejection_no_side_effects(self):
        db = _fresh()
        seed_run(db, "s1", "accA", kernel_ref="accA/k1")
        seed_run(db, "s2", "accA", kernel_ref="accA/k2")
        WD, _ = self._dist()
        res = asyncio_run(
            WD.distribute_and_launch(
                base_title="Timing",
                code_content=CODE,
                filename="main.py",
                accounts=["accA"],
                total_items=10,
                accelerator="nvidia-tesla-t4-x2",
                sessions_per_account=2,
            )
        )
        self.assertFalse(res["success"])
        self.assertIn("No free GPU session slots", res["error"])
        self.assertEqual(len(db.get_all_workloads()), 0)

    def test_4_cpu_runs_do_not_consume_gpu_slots(self):
        db = _fresh()
        seed_run(
            db,
            "c1",
            "accA",
            status="running",
            accelerator="none",
            kernel_ref="accA/cpu-job-1",
        )
        seed_run(
            db,
            "c2",
            "accA",
            status="running",
            accelerator="none",
            kernel_ref="accA/cpu-job-2",
        )
        WD, _ = self._dist()
        res = asyncio_run(
            WD.distribute_and_launch(
                base_title="Timing",
                code_content=CODE,
                filename="main.py",
                accounts=["accA"],
                total_items=10,
                accelerator="nvidia-tesla-t4-x2",
                sessions_per_account=2,
            )
        )
        self.assertTrue(res["success"])
        self.assertEqual(res["total_shards"], 2)

    def test_5_remainder_split_across_runners(self):
        _fresh()
        WD, _ = self._dist()
        res = asyncio_run(
            WD.distribute_and_launch(
                base_title="Rem",
                code_content=CODE,
                filename="main.py",
                accounts=["accA"],
                total_items=7,
                accelerator="nvidia-tesla-t4-x2",
                sessions_per_account=2,
            )
        )
        self.assertTrue(res["success"])
        ordered = sorted(res["shards"], key=lambda s: s["shard_index"])
        self.assertEqual(
            [(s["range"], s["count"]) for s in ordered], [([0, 4], 4), ([4, 7], 3)]
        )

    def test_6_atomic_conflict_rejection(self):
        db = _fresh()
        WD, _ = self._dist()
        from services.kaggle_service import KaggleService

        colliding_ref = f"accB/{KaggleService.sanitize_slug('Timing [Shard 2/2]')}"
        seed_run(db, "blocker", "accB", kernel_ref=colliding_ref)
        res = asyncio_run(
            WD.distribute_and_launch(
                base_title="Timing",
                code_content=CODE,
                filename="main.py",
                accounts=["accA", "accB"],
                total_items=10,
                accelerator="nvidia-tesla-t4-x2",
                sessions_per_account=1,
            )
        )
        self.assertFalse(res["success"])
        self.assertEqual(res.get("status"), "conflict")
        self.assertIn(colliding_ref, res["error"])
        self.assertEqual(len(db.get_all_workloads()), 0)

    def test_7_stop_workload_endpoint(self):
        db = _fresh()
        WD, _ = self._dist()
        res = asyncio_run(
            WD.distribute_and_launch(
                base_title="StopMe",
                code_content=CODE,
                filename="main.py",
                accounts=["accA"],
                total_items=10,
                accelerator="nvidia-tesla-t4-x2",
                sessions_per_account=2,
            )
        )
        wid = res["workload_id"]

        runs = db.get_active_runs()
        targets = [r for r in runs if r.get("workload_id") == wid]
        self.assertEqual(len(targets), 2)
        for r in targets:
            db.update_run_status(r["id"], "running")

        from routers.distributed import stop_workload

        out = asyncio_run(stop_workload(wid))
        self.assertTrue(out["success"])
        self.assertEqual(out["message"], "Stopped 2/2 shards.")
        for r in targets:
            self.assertEqual(db.get_run_by_id(r["id"])["status"], "stopped")
        self.assertEqual(db.get_all_workloads()[0]["status"], "stopped")

    def test_8_stop_workload_with_no_active_shards_404(self):
        _fresh()
        from fastapi import HTTPException
        from routers.distributed import stop_workload

        with self.assertRaises(HTTPException) as cm:
            asyncio_run(stop_workload("workload_does_not_exist"))
        self.assertEqual(cm.exception.status_code, 404)

    def test_8b_all_shards_stuck_reports_409_and_names_them(self):
        db = _fresh()
        from fastapi import HTTPException
        from routers.distributed import stop_workload

        db.create_distributed_workload(
            {
                "id": "wl_stuck",
                "title": "Stuck",
                "workload_type": "auto",
                "total_units": 10,
                "accounts_used": "[]",
                "created_at": "2026-09-29T00:00:00+00:00",
                "status": "running",
            }
        )
        seed_run(
            db,
            "wl_run_stuck",
            "accA",
            status="cancelling",
            kernel_ref="accA/stuck-one",
            workload_id="wl_stuck",
        )

        with self.assertRaises(HTTPException) as cm:
            asyncio_run(stop_workload("wl_stuck"))
        self.assertEqual(cm.exception.status_code, 409)
        detail = cm.exception.detail
        self.assertIn("CANCEL_ACKNOWLEDGED", detail)
        self.assertIn("accA/stuck-one", detail)
        self.assertIn("kaggle kernels delete", detail)

    def test_9_per_account_sessions_map(self):
        db = _fresh()
        WD, _ = self._dist()
        res = asyncio_run(
            WD.distribute_and_launch(
                base_title="MapTest",
                code_content=CODE,
                filename="main.py",
                accounts=["accA", "accB"],
                total_items=10,
                accelerator="nvidia-tesla-t4-x2",
                sessions_per_account={"accA": 1, "accB": 2},
            )
        )
        self.assertTrue(res["success"], res)
        self.assertEqual(res["total_shards"], 3)
        plan = {p["account"]: p for p in res["runner_plan"]}
        self.assertEqual(plan["accA"]["requested"], 1)
        self.assertEqual(plan["accA"]["slots"], 1)
        self.assertEqual(plan["accB"]["requested"], 2)
        self.assertEqual(plan["accB"]["slots"], 2)
        meta = db.get_all_workloads()[0]["accounts_used"]
        self.assertEqual(meta["sessions_per_account"], {"accA": 1, "accB": 2})

    def test_10_per_account_map_clamped_and_missing_defaults(self):
        _fresh()
        WD, _ = self._dist()
        res = asyncio_run(
            WD.distribute_and_launch(
                base_title="ClampTest",
                code_content=CODE,
                filename="main.py",
                accounts=["accA", "accB"],
                total_items=12,
                accelerator="nvidia-tesla-t4-x2",
                sessions_per_account={"accA": 99},
            )
        )
        self.assertTrue(res["success"])
        plan = {p["account"]: p for p in res["runner_plan"]}
        self.assertEqual(plan["accA"]["requested"], 2)
        self.assertEqual(plan["accB"]["requested"], 2)
        self.assertEqual(res["total_shards"], 4)

    def test_11_manual_shards_distribution(self):
        db = _fresh()
        WD, _ = self._dist()
        manual_shards = [
            {"account": "accA", "start_index": 0, "end_index": 100},
            {"account": "accB", "start_index": 100, "end_index": 500},
        ]
        res = asyncio_run(
            WD.distribute_and_launch(
                base_title="ManualShardsTest",
                code_content=CODE,
                filename="main.py",
                accounts=["accA", "accB"],
                total_items=500,
                accelerator="none",
                manual_shards=manual_shards,
            )
        )
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
        _fresh()
        WD, _ = self._dist()
        res = asyncio_run(
            WD.distribute_and_launch(
                base_title="InvalidRange",
                code_content=CODE,
                filename="main.py",
                accounts=["accA"],
                total_items=100,
                manual_shards=[
                    {"account": "accA", "start_index": 200, "end_index": 50}
                ],
            )
        )
        self.assertFalse(res["success"])
        self.assertIn("greater than end index", res["error"])

        res2 = asyncio_run(
            WD.distribute_and_launch(
                base_title="MissingAccount",
                code_content=CODE,
                filename="main.py",
                accounts=[],
                total_items=100,
                manual_shards=[{"account": "", "start_index": 0, "end_index": 50}],
            )
        )
        self.assertFalse(res2["success"])
        self.assertIn("missing a target Kaggle account", res2["error"])

    def test_13_accounts_descending_quota_sort(self):
        db = _fresh()
        from routers.accounts import list_accounts

        db.save_account(
            "1",
            "acc_low",
            "key1",
            {"gpu": {"limit": 30, "used": 25}, "tpu": {"limit": 20, "used": 0}},
        )
        db.save_account(
            "2",
            "acc_high",
            "key2",
            {"gpu": {"limit": 30, "used": 2}, "tpu": {"limit": 20, "used": 0}},
        )
        db.save_account(
            "3",
            "acc_mid",
            "key3",
            {"gpu": {"limit": 30, "used": 15}, "tpu": {"limit": 20, "used": 0}},
        )

        resp = asyncio_run(list_accounts())
        self.assertTrue(resp["success"])
        names = [a["username"] for a in resp["accounts"]]
        self.assertEqual(names, ["acc_high", "acc_mid", "acc_low"])

    def test_14_long_base_title_keeps_shards_distinct(self):
        db = _fresh()
        WD, _ = self._dist()
        base = "Distributed Llm extraction pipeline.kaggle"
        self.assertEqual(len(base), 42)
        res = asyncio_run(
            WD.distribute_and_launch(
                base_title=base,
                code_content=CODE,
                filename="main.py",
                accounts=["accA", "accB"],
                total_items=10,
                accelerator="nvidia-tesla-t4-x2",
                sessions_per_account=2,
            )
        )
        self.assertTrue(res["success"], res)
        self.assertEqual(res["total_shards"], 4)
        self.assertEqual(res["status"], "dispatched")
        pushed = [s for s in res["shards"] if s.get("push_result", {}).get("success")]
        self.assertEqual(len(pushed), 4)
        refs = [r["kernel_ref"] for r in db.get_all_runs(limit=10)]
        self.assertEqual(len(refs), 4)
        self.assertEqual(len(set(refs)), 4)
        titles = [r["title"] for r in db.get_all_runs(limit=10)]
        self.assertTrue(all(len(t) <= 50 for t in titles))
        self.assertTrue(all("[Shard " in t for t in titles))


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)


if __name__ == "__main__":
    unittest.main(verbosity=2)
