import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TEST_DIR)
sys.path.insert(0, TEST_DIR)
sys.path.insert(0, os.path.join(REPO_ROOT, "app"))

DATA_TMP = None


def setUpModule():
    global DATA_TMP
    DATA_TMP = tempfile.mkdtemp(prefix="arch_fixes_")
    os.environ["AUTOMATION_DATA_DIR"] = DATA_TMP


def tearDownModule():
    os.environ.pop("AUTOMATION_DATA_DIR", None)
    if DATA_TMP and os.path.isdir(DATA_TMP):
        shutil.rmtree(DATA_TMP, ignore_errors=True)


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


if __name__ == "__main__":
    unittest.main()
