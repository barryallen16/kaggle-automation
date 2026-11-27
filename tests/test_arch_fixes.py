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


if __name__ == "__main__":
    unittest.main()
