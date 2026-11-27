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
    _ENV_KEYS = ("KABILA_API_URL", "KABILA_APP_AUTH_TOKEN", "APP_AUTH_TOKEN")

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="kaggle_runner_test_"))
        self.orig_cwd = Path.cwd()
        self._saved_env = {k: os.environ.pop(k, None) for k in self._ENV_KEYS}
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self.orig_cwd)
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
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
                {"status": "ok"},
                {"success": True, "accounts": [{"username": "acc1"}]},
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
            {"status": "ok", "service": "kaggle-nb-automation"},
            {"success": True, "accounts": [{"username": "acc1", "active_gpu_runs": 1}]},
            {"success": True, "active_runs": [{"id": "r1", "title": "t1", "account_username": "acc1", "status": "running"}]},
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

    @patch("kaggle_runner.api_request")
    def test_aggregate_accounts_uses_real_api_shape(self, mock_api):
        mock_api.side_effect = [
            {"status": "ok"},
            {
                "success": True,
                "accounts": [
                    {
                        "username": "realuser",
                        "active_runs": [
                            {"id": "r1", "accelerator": "nvidia-tesla-t4-x2"},
                            {"id": "r2", "accelerator": "none"},
                        ],
                        "last_quota": {"gpu": {"used": 5.5, "limit": 30}},
                    }
                ],
            },
            {"success": True, "active_runs": []},
        ]
        args = MagicMock(url=None, token=None)
        with patch("builtins.print") as mock_print:
            ret = kaggle_runner.cmd_status(args)
            self.assertEqual(ret, 0)
            printed = "\n".join(str(c[0][0]) for c in mock_print.call_args_list)
            self.assertIn("total_free_gpu_slots: 1", printed)
            self.assertIn("24.5", printed)

    @patch("kaggle_runner.download_to_file")
    @patch("kaggle_runner.api_request")
    def test_cmd_outputs_list_pull_and_download(self, mock_api, mock_dl):
        mock_api.return_value = {
            "success": True,
            "remote_files": [{"name": "out.jsonl", "size": "123"}],
            "local_files": [],
        }
        args = MagicMock(
            run_id="run_1",
            pull=False,
            download=None,
            zip=False,
            out=None,
            url=None,
            token=None,
        )
        with patch("builtins.print") as mock_print:
            self.assertEqual(kaggle_runner.cmd_outputs(args), 0)
            printed = "\n".join(str(c[0][0]) for c in mock_print.call_args_list)
            self.assertIn("out.jsonl", printed)

        mock_api.return_value = {"success": True, "message": "Downloaded 1 file(s)"}
        mock_dl.return_value = {"success": True, "path": str(self.tmp / "out.jsonl"), "bytes": 123}
        args2 = MagicMock(
            run_id="run_1",
            pull=True,
            download="out.jsonl",
            zip=False,
            out=str(self.tmp / "out.jsonl"),
            url=None,
            token=None,
        )
        with patch("builtins.print"):
            self.assertEqual(kaggle_runner.cmd_outputs(args2), 0)
        mock_api.assert_called_with(
            kaggle_runner.DEFAULT_URL,
            "/api/runs/run_1/files/pull",
            token="",
            method="POST",
            timeout=600,
        )
        mock_dl.assert_called_once()

    def test_cmd_outputs_rejects_zip_and_download_together(self):
        args = MagicMock(
            run_id="run_1",
            pull=False,
            download="out.jsonl",
            zip=True,
            out=None,
            url=None,
            token=None,
        )
        with patch("builtins.print"), self.assertRaises(SystemExit) as cm:
            kaggle_runner.cmd_outputs(args)
        self.assertEqual(cm.exception.code, 1)

    @patch("kaggle_runner.api_request")
    def test_home_status_and_run_fail_on_401_after_health_200(self, mock_api):
        auth_err = {
            "error": "Authentication failed (HTTP 401): Unauthorized",
            "http_status": 401,
            "help": "Pass --token <key> or run `kaggle_runner auth --token <key>`",
        }
        mock_api.side_effect = [{"status": "ok"}, auth_err]
        with patch("builtins.print") as mock_print, self.assertRaises(SystemExit) as cm:
            kaggle_runner.cmd_home("https://kabila-aws.isroot.in", "")
        self.assertEqual(cm.exception.code, 1)
        printed = "\n".join(str(c[0][0]) for c in mock_print.call_args_list)
        self.assertIn("Authentication failed", printed)
        self.assertIn("--token", printed)

        mock_api.side_effect = [{"status": "ok"}, auth_err]
        with patch("builtins.print") as mock_print, self.assertRaises(SystemExit) as cm:
            kaggle_runner.cmd_status(MagicMock(url=None, token=None))
        self.assertEqual(cm.exception.code, 1)
        printed = "\n".join(str(c[0][0]) for c in mock_print.call_args_list)
        self.assertIn("Authentication failed", printed)

        script = self.tmp / "job.py"
        script.write_text("print(1)\n", encoding="utf-8")
        mock_api.side_effect = [auth_err]
        run_args = MagicMock(
            file=str(script),
            title="t",
            account=None,
            accelerator="nvidia-tesla-t4-x2",
            no_internet=False,
            trial=False,
            timeout=None,
            url=None,
            token=None,
        )
        with patch("builtins.print") as mock_print, self.assertRaises(SystemExit) as cm:
            kaggle_runner.cmd_run(run_args)
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("Authentication failed", "\n".join(str(c[0][0]) for c in mock_print.call_args_list))

    @patch("kaggle_runner.api_request")
    def test_accelerator_allows_new_gpus_and_tpus(self, mock_api):
        script = self.tmp / "job.py"
        script.write_text("print(1)\n", encoding="utf-8")
        for accel in ("nvidia-l4", "nvidia-a100", "nvidia-h100", "tpu-v5e-8"):
            mock_api.return_value = {"success": True, "run_id": "r1"}
            with (
                patch.object(sys, "argv", ["kaggle_runner", "run", str(script), "--account", "acc1", "--accelerator", accel]),
                patch("builtins.print"),
            ):
                self.assertEqual(kaggle_runner.main(), 0)
            mock_api.return_value = {"success": True, "workload_id": "w1"}
            with (
                patch.object(sys, "argv", ["kaggle_runner", "distributed", str(script), "--title", "t", "--accounts", "acc1", "--accelerator", accel]),
                patch("builtins.print"),
            ):
                self.assertEqual(kaggle_runner.main(), 0)

    @patch("kaggle_runner.api_request")
    def test_run_distributed_stop_fail_on_success_false_without_error(self, mock_api):
        script = self.tmp / "job.py"
        script.write_text("print(1)\n", encoding="utf-8")

        mock_api.return_value = {"success": False, "run_id": "r1", "status": "session_cap", "message": "all slots held"}
        run_args = MagicMock(
            file=str(script),
            title="t",
            account="acc1",
            accelerator="nvidia-l4",
            no_internet=False,
            trial=False,
            timeout=None,
            url=None,
            token=None,
        )
        with patch("builtins.print") as mock_print, self.assertRaises(SystemExit) as cm:
            kaggle_runner.cmd_run(run_args)
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("all slots held", "\n".join(str(c[0][0]) for c in mock_print.call_args_list))

        mock_api.return_value = {
            "success": False,
            "workload_id": "w1",
            "status": "failed",
            "shards_pushed": 0,
            "shards": [{"push_result": {"success": False, "message": "quota exhausted"}}],
        }
        dist_args = MagicMock(
            file=str(script),
            title="t",
            accounts="acc1",
            total_items=10,
            start_offset=0,
            accelerator="nvidia-l4",
            no_internet=False,
            trial=False,
            timeout=None,
            sessions_per_account=1,
            url=None,
            token=None,
        )
        with patch("builtins.print") as mock_print, self.assertRaises(SystemExit) as cm:
            kaggle_runner.cmd_distributed(dist_args)
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("quota exhausted", "\n".join(str(c[0][0]) for c in mock_print.call_args_list))

        for payload, expected in (
            ({"success": False, "detail": "Stop is already in progress"}, "Stop is already in progress"),
            ({"success": False, "message": "Stopped 0/2 shards.", "failed": []}, "Stopped 0/2 shards."),
        ):
            mock_api.return_value = payload
            stop_args = MagicMock(target_id="w 1", workload=True, url=None, token=None)
            with patch("builtins.print") as mock_print, self.assertRaises(SystemExit) as cm:
                kaggle_runner.cmd_stop(stop_args)
            self.assertEqual(cm.exception.code, 1)
            self.assertIn(expected, "\n".join(str(c[0][0]) for c in mock_print.call_args_list))

    @patch("kaggle_runner.api_request")
    def test_home_and_status_reject_non_json_200_responses(self, mock_api):
        mock_api.return_value = "<html>login</html>"
        with patch("builtins.print") as mock_print, self.assertRaises(SystemExit) as cm:
            kaggle_runner.cmd_home("https://kabila-aws.isroot.in", "")
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("non-JSON", "\n".join(str(c[0][0]) for c in mock_print.call_args_list))

    def test_real_fastapi_auth_middleware_401_and_bearer_200(self):
        import importlib
        import io
        import urllib.error

        import config
        import database
        import main
        from fastapi.testclient import TestClient

        prev_data_dir = os.environ.get("AUTOMATION_DATA_DIR")
        os.environ["AUTOMATION_DATA_DIR"] = str(self.tmp)
        importlib.reload(config)
        importlib.reload(database)
        importlib.reload(main)
        try:
            def fake_urlopen(req, timeout=45):
                path = req.full_url.split("http://testserver", 1)[-1] or "/"
                r = client.request(req.get_method(), path, headers=dict(req.header_items()), content=req.data)
                if r.status_code >= 400:
                    raise urllib.error.HTTPError(req.full_url, r.status_code, r.reason_phrase, r.headers, io.BytesIO(r.content))
                resp = MagicMock()
                resp.headers = r.headers
                resp.read.return_value = r.content
                resp.__enter__ = lambda s: s
                resp.__exit__ = lambda s, *a: False
                return resp

            with (
                patch.object(main, "APP_AUTH_TOKEN", "secret-token"),
                patch("urllib.request.urlopen", side_effect=fake_urlopen),
                TestClient(main.app) as client,
            ):
                with patch("builtins.print") as mock_print, self.assertRaises(SystemExit) as cm:
                    kaggle_runner.cmd_home("http://testserver", "")
                self.assertEqual(cm.exception.code, 1)
                self.assertIn("401", "\n".join(str(c[0][0]) for c in mock_print.call_args_list))

                with patch("builtins.print") as mock_print:
                    self.assertEqual(kaggle_runner.cmd_home("http://testserver", "secret-token"), 0)
                self.assertIn("status: ok", "\n".join(str(c[0][0]) for c in mock_print.call_args_list))
        finally:
            if prev_data_dir is not None:
                os.environ["AUTOMATION_DATA_DIR"] = prev_data_dir
            else:
                os.environ.pop("AUTOMATION_DATA_DIR", None)
            importlib.reload(config)
            importlib.reload(database)

    @patch("kaggle_runner.api_request")
    def test_logs_and_stop_url_quote_ids(self, mock_api):
        mock_api.return_value = "ok"
        with patch("builtins.print"):
            kaggle_runner.cmd_logs(MagicMock(run_id="run/1 2", remote=False, tail=10, full=True, url=None, token=None))
        self.assertEqual(mock_api.call_args[0][1], "/api/runs/run%2F1%202/logs?fetch_remote=false")

        mock_api.return_value = {"success": True}
        with patch("builtins.print"):
            kaggle_runner.cmd_stop(MagicMock(target_id="wk/1 2", workload=True, url=None, token=None))
        self.assertEqual(mock_api.call_args[0][1], "/api/distributed/wk%2F1%202/stop")
        self.assertEqual(mock_api.call_args.kwargs.get("timeout"), 240)


if __name__ == "__main__":
    unittest.main()
