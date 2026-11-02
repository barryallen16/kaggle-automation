"""Tests for skills/kaggle-runner/scripts/kaggle_runner.py with AXI enhancements."""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "skills" / "kaggle-runner" / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "app"))

import auth
import axi
import kaggle_runner


class TestKaggleRunnerCLI(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="kaggle_runner_test_"))
        self.orig_cwd = Path.cwd()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self.orig_cwd)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_session_cookie_matches_backend(self):
        token = "test-secret-token-123"
        cookie = kaggle_runner.make_session_cookie(token)
        self.assertTrue(auth.verify_session_cookie(cookie, token))
        self.assertFalse(auth.verify_session_cookie(cookie, "wrong-token"))

    def test_auth_ensures_gitignore_and_writes_env(self):
        token = "auth-token-xyz"
        args = MagicMock(token=token, url="https://kabila-aws.isroot.in")

        with patch("kaggle_runner.api_request") as mock_api:
            mock_api.side_effect = [
                {"status": "ok"},  # /api/health
                {"success": True, "accounts": [{"username": "acc1"}]},  # /api/accounts
            ]
            ret = kaggle_runner.cmd_auth(args)
            self.assertEqual(ret, 0)

        gitignore = self.tmp / ".gitignore"
        env = self.tmp / ".env"
        self.assertTrue(gitignore.exists())
        self.assertTrue(env.exists())

        gi_content = gitignore.read_text(encoding="utf-8")
        self.assertIn(".env", gi_content)

        env_content = env.read_text(encoding="utf-8")
        self.assertIn(f"KABILA_APP_AUTH_TOKEN={token}", env_content)
        self.assertIn("KABILA_API_URL=https://kabila-aws.isroot.in", env_content)

    def test_auth_idempotent_no_op(self):
        env = self.tmp / ".env"
        env.write_text("KABILA_API_URL=https://kabila-aws.isroot.in\nKABILA_APP_AUTH_TOKEN=secret\n", encoding="utf-8")
        args = MagicMock(token="secret", url="https://kabila-aws.isroot.in")

        with patch("builtins.print") as mock_print:
            ret = kaggle_runner.cmd_auth(args)
            self.assertEqual(ret, 0)
            printed = "\n".join(str(c[0][0]) for c in mock_print.call_args_list)
            self.assertIn("already configured (no-op)", printed)

    def test_load_config_from_env(self):
        env = self.tmp / ".env"
        env.write_text("KABILA_API_URL=https://custom.isroot.in\nKABILA_APP_AUTH_TOKEN=my-secret\n", encoding="utf-8")

        url, token = kaggle_runner.load_config()
        self.assertEqual(url, "https://custom.isroot.in")
        self.assertEqual(token, "my-secret")

    def test_build_multipart_payload(self):
        fields = {"title": "Test Title", "accelerator": "nvidia-tesla-t4-x2", "enable_internet": True}
        files = {"file": ("script.py", b"print('hello world')")}
        body, content_type = kaggle_runner._build_multipart(fields, files)

        self.assertTrue(content_type.startswith("multipart/form-data; boundary="))
        self.assertIn(b'name="title"', body)
        self.assertIn(b"Test Title", body)
        self.assertIn(b'name="enable_internet"', body)
        self.assertIn(b"true", body)
        self.assertIn(b'filename="script.py"', body)
        self.assertIn(b"print('hello world')", body)

    @patch("kaggle_runner.api_request")
    def test_cmd_status_toon_output(self, mock_api):
        mock_api.side_effect = [
            {"status": "ok", "service": "kaggle-nb-automation"},  # health
            {"success": True, "accounts": [{"username": "acc1", "active_gpu_runs": 1}]},  # accounts
            {"success": True, "active_runs": [{"id": "r1", "title": "t1", "account_username": "acc1", "status": "running"}]},  # active
        ]
        args = MagicMock(url=None, token=None)
        with patch("builtins.print") as mock_print:
            ret = kaggle_runner.cmd_status(args)
            self.assertEqual(ret, 0)
            printed = "\n".join(str(c[0][0]) for c in mock_print.call_args_list)
            self.assertIn("total_free_gpu_slots: 1", printed)
            self.assertIn("help[", printed)

    @patch("kaggle_runner.api_request")
    def test_cmd_home_content_first(self, mock_api):
        mock_api.side_effect = [
            {"status": "ok", "service": "kaggle-nb-automation"},
            {"success": True, "accounts": [{"username": "acc1", "active_gpu_runs": 0}]},
            {"success": True, "active_runs": []},
        ]
        with patch("builtins.print") as mock_print:
            ret = kaggle_runner.cmd_home("https://kabila-aws.isroot.in", "secret")
            self.assertEqual(ret, 0)
            printed = "\n".join(str(c[0][0]) for c in mock_print.call_args_list)
            self.assertIn("bin:", printed)
            self.assertIn("total_free_gpu_slots: 2", printed)
            self.assertIn("help[", printed)

    @patch("kaggle_runner.api_request")
    def test_cmd_logs_truncation(self, mock_api):
        mock_api.return_value = "\n".join(f"line {i}" for i in range(150))
        args = MagicMock(run_id="r1", remote=False, tail=50, full=False, url=None, token=None)
        with patch("builtins.print") as mock_print:
            ret = kaggle_runner.cmd_logs(args)
            self.assertEqual(ret, 0)
            printed = "\n".join(str(c[0][0]) for c in mock_print.call_args_list)
            self.assertIn("truncated, 150 lines total", printed)
            self.assertIn("--full", printed)

    def test_axi_parser_fails_loud_on_stdout(self):
        parser = axi.AxiParser(prog="test_prog")
        parser.add_argument("--count", type=int)

        with patch("builtins.print") as mock_print, self.assertRaises(SystemExit) as cm:
            parser.parse_args(["--unknown-flag"])

        self.assertEqual(cm.exception.code, 2)
        printed = "\n".join(str(c[0][0]) for c in mock_print.call_args_list)
        self.assertIn("error:", printed)
        self.assertIn("valid flags for `test_prog`: --count", printed)


if __name__ == "__main__":
    unittest.main()
