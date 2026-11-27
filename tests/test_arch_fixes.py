import asyncio
import importlib
import io
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

import httpx

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TEST_DIR)
sys.path.insert(0, TEST_DIR)
sys.path.insert(0, os.path.join(REPO_ROOT, "app"))

DATA_TMP = None
_PREV_DATA_DIR = None


def setUpModule():
    global DATA_TMP, _PREV_DATA_DIR
    _PREV_DATA_DIR = os.environ.get("AUTOMATION_DATA_DIR")
    DATA_TMP = tempfile.mkdtemp(prefix="arch_fixes_")
    os.environ["AUTOMATION_DATA_DIR"] = DATA_TMP


def tearDownModule():
    if _PREV_DATA_DIR is not None:
        os.environ["AUTOMATION_DATA_DIR"] = _PREV_DATA_DIR
    else:
        os.environ.pop("AUTOMATION_DATA_DIR", None)
    import config
    import database

    importlib.reload(config)
    importlib.reload(database)
    if DATA_TMP and os.path.isdir(DATA_TMP):
        shutil.rmtree(DATA_TMP, ignore_errors=True)


class TestAuthMiddlewareBlocksBeforeHandler(unittest.TestCase):
    def test_unauthenticated_api_request_never_executes_route(self):
        import main
        from fastapi.testclient import TestClient

        called = []

        @main.app.post("/api/_test_side_effect")
        async def _side_effect():
            called.append(True)
            return {"ok": True}

        with mock.patch.object(main, "APP_AUTH_TOKEN", "super-secret-token"):
            client = TestClient(main.app)
            r_unauth = client.post("/api/_test_side_effect")
            self.assertEqual(r_unauth.status_code, 401)
            self.assertEqual(called, [], "route handler ran before auth check")

            r_bearer = client.post(
                "/api/_test_side_effect",
                headers={"Authorization": "Bearer super-secret-token"},
            )
            self.assertEqual(r_bearer.status_code, 200)
            self.assertEqual(called, [True])

            import auth
            from starlette.requests import Request

            raw_req = Request(
                {
                    "type": "http",
                    "headers": [(b"authorization", "Bearer \u2603".encode("utf-8"))],
                }
            )
            self.assertFalse(auth.is_request_authenticated(raw_req, "super-secret-token"))

            r_login_non_ascii = client.post(
                "/login",
                data={"access_token": "\u2603"},
                follow_redirects=False,
            )
            self.assertEqual(r_login_non_ascii.status_code, 303)
            self.assertIn("Invalid+access+token", r_login_non_ascii.headers.get("location", ""))


class TestStreamingVersionedZip(unittest.TestCase):
    def test_302_redirect_streams_zip_to_disk_and_blocks_zip_slip(self):
        import services.kaggle_versioned_output as vo

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("checkpoints/model.bin", b"model-bytes")
            zf.writestr("../../evil.txt", b"escape")
        zip_bytes = buf.getvalue()

        def handler(request):
            if request.url.path.endswith("DownloadKernelOutput"):
                return httpx.Response(
                    302, headers={"location": "/v1/kernels/output/download_zip/999"}
                )
            if request.url.path.endswith("/download_zip/999"):
                return httpx.Response(
                    200,
                    content=zip_bytes,
                    headers={"content-type": "application/zip"},
                )
            return httpx.Response(404)

        out_dir = tempfile.mkdtemp(prefix="zip_stream_")
        try:
            with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                saved = vo._download_via_kernel_output_api(
                    client, "owner", "slug", 1, out_dir
                )
            self.assertEqual(saved, ["checkpoints/model.bin"])
            self.assertTrue((Path(out_dir) / "checkpoints" / "model.bin").is_file())
            self.assertFalse((Path(out_dir).parent.parent / "evil.txt").exists())
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)

    def test_cross_origin_redirect_strips_authorization_header(self):
        import services.kaggle_versioned_output as vo

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("result.json", b"{}")
        zip_bytes = buf.getvalue()

        seen_headers: list[tuple[str, str | None]] = []

        def handler(request):
            seen_headers.append((str(request.url), request.headers.get("authorization")))
            if str(request.url).startswith("https://api.kaggle.com"):
                return httpx.Response(
                    302,
                    headers={"location": "https://storage.googleapis.com/kaggle-out/out.zip"},
                )
            if str(request.url).startswith("https://storage.googleapis.com"):
                return httpx.Response(
                    200,
                    content=zip_bytes,
                    headers={"content-type": "application/zip"},
                )
            return httpx.Response(404)

        out_dir = tempfile.mkdtemp(prefix="zip_xorigin_")
        try:
            with httpx.Client(
                transport=httpx.MockTransport(handler),
                headers={"Authorization": "Bearer top-secret-kaggle-token"},
            ) as client:
                saved = vo._download_via_kernel_output_api(
                    client, "owner", "slug", 1, out_dir
                )
            self.assertEqual(saved, ["result.json"])
            self.assertEqual(len(seen_headers), 2)
            self.assertEqual(seen_headers[0][1], "Bearer top-secret-kaggle-token")
            self.assertIsNone(seen_headers[1][1])
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)


class TestCancellingReap(unittest.TestCase):
    def test_reaps_cancelling_when_reserved_quota_is_zero(self):
        import config
        import database as db
        import services.account_manager as am
        import services.kaggle_service as ks
        import services.session_monitor as sm

        importlib.reload(config)
        importlib.reload(db)
        db.init_db()
        db.save_account("1", "acct", "tok")
        db.create_run_record(
            {
                "id": "run_c1",
                "account_username": "acct",
                "kernel_slug": "slug",
                "kernel_ref": "acct/slug",
                "title": "Stuck",
                "code_file": "",
                "accelerator": "nvidia-tesla-t4-x2",
                "enable_internet": 1,
                "is_trial": 0,
                "timeout_seconds": 43200,
                "status": "cancelling",
                "status_message": "",
                "start_time": "2026-11-24T00:00:00+00:00",
                "kaggle_url": "https://www.kaggle.com/code/acct/slug",
                "workload_id": None,
                "shard_index": None,
                "total_shards": None,
                "log_file": "",
            }
        )

        async def fake_refresh(user):
            return {"gpu": {"used": 12.0, "limit": 30.0, "reserved_seconds": 0.0}}

        async def fake_status(user, ref):
            return {"success": True, "status": "cancelling", "raw": "CANCEL_ACKNOWLEDGED"}

        with (
            mock.patch.object(
                am.AccountManager, "refresh_account_quota", staticmethod(fake_refresh)
            ),
            mock.patch.object(
                ks.KaggleService, "get_kernel_status", staticmethod(fake_status)
            ),
        ):
            asyncio.run(
                sm.SessionMonitor.reap_stale_cancelling_runs(
                    now=datetime(2026, 11, 27, 12, 1, tzinfo=UTC)
                )
            )

        row = db.get_run_by_id("run_c1")
        self.assertEqual(row["status"], "stopped")
        self.assertEqual(db.get_session_holding_runs(), [])

    def test_reap_respects_accelerator_type(self):
        import config
        import database as db
        import services.account_manager as am
        import services.kaggle_service as ks
        import services.session_monitor as sm

        importlib.reload(config)
        importlib.reload(db)
        db.init_db()
        db.save_account("2", "acct2", "tok2")
        for rid, slug, acc in [
            ("run_cpu", "slug-cpu", "none"),
            ("run_tpu", "slug-tpu", "tpu-v3-8"),
        ]:
            db.create_run_record(
                {
                    "id": rid,
                    "account_username": "acct2",
                    "kernel_slug": slug,
                    "kernel_ref": f"acct2/{slug}",
                    "title": slug,
                    "code_file": "",
                    "accelerator": acc,
                    "enable_internet": 1,
                    "is_trial": 0,
                    "timeout_seconds": 43200,
                    "status": "cancelling",
                    "status_message": "",
                    "start_time": "2026-11-27T12:00:00+00:00",
                    "end_time": "2026-11-27T12:00:00+00:00",
                    "kaggle_url": f"https://www.kaggle.com/code/acct2/{slug}",
                    "workload_id": None,
                    "shard_index": None,
                    "total_shards": None,
                    "log_file": "",
                }
            )

        async def fake_refresh(user):
            return {
                "gpu": {"used": 0.0, "limit": 30.0, "reserved_seconds": 0.0},
                "tpu": {"used": 0.0, "limit": 20.0, "reserved_seconds": 0.0},
            }

        async def fake_status(user, ref):
            return {"success": True, "status": "cancelling", "raw": "CANCEL_ACKNOWLEDGED"}

        with (
            mock.patch.object(
                am.AccountManager, "refresh_account_quota", staticmethod(fake_refresh)
            ),
            mock.patch.object(
                ks.KaggleService, "get_kernel_status", staticmethod(fake_status)
            ),
        ):
            asyncio.run(
                sm.SessionMonitor.reap_stale_cancelling_runs(
                    now=datetime(2026, 11, 27, 12, 1, tzinfo=UTC)
                )
            )

        self.assertEqual(db.get_run_by_id("run_cpu")["status"], "cancelling")
        self.assertEqual(db.get_run_by_id("run_tpu")["status"], "stopped")
        db.update_run_status("run_cpu", "stopped", "test cleanup")


class TestDistributedPushNoDeadlock(unittest.TestCase):
    def test_three_shards_acquire_push_semaphore_without_deadlock(self):
        import config
        import database as db
        import services.account_manager as am
        import services.kaggle_service as ks
        import services.workload_distributor as wd

        importlib.reload(config)
        importlib.reload(db)
        db.init_db()
        for i in range(1, 4):
            db.save_account(f"d{i}", f"duser{i}", f"dtok{i}")

        async def fake_reclaim(accounts):
            return {"cancelled": [], "checked": len(accounts)}

        async def fake_busy(accounts, is_gpu):
            return {a: 0 for a in accounts}

        async def fake_budget(acc, concurrent):
            return None

        async def fake_push(**kwargs):
            async with ks.KaggleService._get_push_semaphore():
                await asyncio.sleep(0.01)
            return {"success": True, "kernel_ref": f"{kwargs['account_username']}/s"}

        with (
            mock.patch.object(
                wd.WorkloadDistributor, "reclaim_slots", staticmethod(fake_reclaim)
            ),
            mock.patch.object(
                wd.WorkloadDistributor, "_busy_gpu_sessions", staticmethod(fake_busy)
            ),
            mock.patch.object(
                am.AccountManager, "gpu_runtime_budget_minutes", staticmethod(fake_budget)
            ),
            mock.patch.object(
                ks.KaggleService, "push_kernel", staticmethod(fake_push)
            ),
        ):
            res = asyncio.run(
                asyncio.wait_for(
                    wd.WorkloadDistributor.distribute_and_launch(
                        accounts=["duser1", "duser2", "duser3"],
                        base_title="NoDeadlock",
                        code_content="print(1)",
                        filename="run.py",
                        total_items=30,
                        sessions_per_account=1,
                    ),
                    timeout=3.0,
                )
            )
        self.assertTrue(res["success"], res)
        self.assertEqual(res["shards_pushed"], 3)


class TestLogFollowerAndCpuSweep(unittest.TestCase):
    def test_ensure_log_stream_skips_duplicate_during_backoff(self):
        import services.kaggle_logs as kl

        starts = 0
        release = None

        async def fake_stream(run_id, acc, ref, log_file):
            nonlocal starts
            starts += 1
            await release.wait()

        run = {
            "id": "run_backoff_1",
            "account_username": "u1",
            "kernel_ref": "u1/k1",
            "status": "running",
            "log_file": str(Path(DATA_TMP) / "run_backoff_1.log"),
        }

        async def scenario():
            nonlocal release
            release = asyncio.Event()
            with mock.patch.object(kl, "start_background_log_stream", fake_stream):
                kl.ensure_log_stream(run)
                kl.ensure_log_stream(run)
                await asyncio.sleep(0)
                kl.ensure_log_stream(run)
                self.assertEqual(starts, 1)
                self.assertIn(run["id"], kl._active_stream_tasks)
                kl.stop_log_stream(run["id"])
                await asyncio.sleep(0)
                self.assertNotIn(run["id"], kl._active_stream_tasks)

        asyncio.run(scenario())

    def test_cpu_sweep_preview_ignores_busy_gpu_slots(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        import json
        import subprocess

        js_path = Path(REPO_ROOT) / "app" / "static" / "js" / "distributed.js"
        script = f"""
const fs = require('fs');
const vm = require('vm');
const preview = {{ innerHTML: '' }};
const summaryEl = {{ textContent: '' }};
const statusEl = {{ innerHTML: '' }};
const elements = {{
  'dist-total-items': {{ value: '100' }},
  'dist-shards-preview': preview,
  'dist-accelerator': {{ value: 'none' }},
  'dist-manual-total-items': summaryEl,
  'dist-manual-validation-status': statusEl,
}};
const ctx = {{
  document: {{
    getElementById: (id) => elements[id] || null,
    querySelectorAll: (sel) => {{
      if (sel === 'input[name="dist-acc"]:checked') return [{{ value: 'acc1' }}];
      if (sel === '.dist-session-select') return [{{ dataset: {{ acc: 'acc1' }}, value: '2' }}];
      return [];
    }},
  }},
  window: {{}},
  AppState: {{ accounts: [{{ username: 'acc1' }}] }},
  QuotaAdapter: {{
    MAX_SESSIONS: 2,
    kindFor: (v) => (v && v !== 'none' ? 'gpu' : null),
    remaining: () => ({{ gpuLeft: 0, tpuLeft: 0 }}),
    busy: () => 2,
  }},
  busyForAccount: () => ({{ busy: 2 }}),
  esc: (s) => String(s),
  refreshIcons: () => {{}},
  showToast: () => {{}},
  Math, Number, Array, Set, Object, JSON, parseInt, Infinity,
}};
vm.runInNewContext(fs.readFileSync({json.dumps(str(js_path))}, 'utf8'), ctx);
ctx.updateShardsPreview();
ctx.populateManualFromAuto(false);
const manualCount = vm.runInNewContext('manualShardsList.length', ctx);
console.log(JSON.stringify({{ html: preview.innerHTML, manualCount }}));
"""
        proc = subprocess.run([node, "-e", script], capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertIn("100 units across 2 runners", out["html"])
        self.assertNotIn("No free GPU session slots", out["html"])
        self.assertEqual(out["manualCount"], 2)


class TestWebsocketRunIdValidation(unittest.TestCase):
    def test_invalid_run_id_rejected_even_with_skip_initial(self):
        import main
        from fastapi.testclient import TestClient
        from starlette.websockets import WebSocketDisconnect

        with mock.patch.object(main, "APP_AUTH_TOKEN", ""):
            client = TestClient(main.app)
            with (
                self.assertRaises(WebSocketDisconnect) as ctx,
                client.websocket_connect("/ws/runs/bad.id/logs?skip_initial=true"),
            ):
                pass
            self.assertEqual(ctx.exception.code, 1008)

            with client.websocket_connect("/ws/runs/valid_run-1/logs?skip_initial=true") as ws:
                self.assertEqual(ws.receive_text(), "")


if __name__ == "__main__":
    unittest.main()
