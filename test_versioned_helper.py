"""Tests for kernel-status normalization and the raw-HTTP versioned fetcher.

The helper must never trust a 'log-only' output page (the signature of the
stop-stub/latest trap), must try every plausible versionLabel spelling, and
status parsing must fold enum-style CLI output into our five statuses.
"""

import os
import sys
import json
import shutil
import tempfile
import unittest

import httpx

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TEST_DIR)

DATA_TMP = tempfile.mkdtemp(prefix="ver_helper_")
os.environ["AUTOMATION_DATA_DIR"] = DATA_TMP
os.environ["KAGGLE_API_TOKEN"] = "test-token"


def setUpModule():
    pass


def tearDownModule():
    shutil.rmtree(DATA_TMP, ignore_errors=True)
    os.environ.pop("AUTOMATION_DATA_TOKEN", None)


def _svc():
    import app.services.kaggle_service as ks
    return ks.KaggleService


class TestStatusNormalization(unittest.TestCase):
    def test_mappings(self):
        n = _svc()._normalize_kernel_status
        self.assertEqual(n("running"), "running")
        self.assertEqual(n("QUEUED"), "queued")
        self.assertEqual(n("complete"), "complete")
        self.assertEqual(n('acc/slug has status "error"'), "error")
        # The live-server leak: raw enum names must become 'stopped'
        self.assertEqual(n("kernelworkerstatus.cancel_acknowledged"), "stopped")
        self.assertEqual(n("CANCEL_ACKNOWLEDGED"), "stopped")
        self.assertEqual(n("canceled"), "stopped")
        self.assertEqual(n(""), "unknown")


class TestVersionedHelper(unittest.TestCase):
    def _run_fetch(self, handler, out_dir):
        import httpx
        import app.services.kaggle_versioned_output as helper

        real_client = httpx.Client
        captured = {}

        def fake_client(*a, **kw):
            kw["transport"] = httpx.MockTransport(handler)
            c = real_client(*a, **kw)
            captured["client"] = c
            return c

        helper.httpx.Client = staticmethod(fake_client)
        try:
            code = helper.main(["fetch", "owner", "my-slug", 7, out_dir])
        finally:
            helper.httpx.Client = real_client
        return code

    def test_1_log_only_pages_rejected_files_accepted(self):
        out_dir = tempfile.mkdtemp(prefix="vh_out_")
        seen_labels = []
        seen_urls = []

        def handler(request: "httpx.Request") -> "httpx.Response":
            body = json.loads(request.content or b"{}")
            seen_urls.append(str(request.url))
            if request.url.path.startswith("/f/"):
                return httpx.Response(200, content=b"REALDATA")
            if request.url.path == "/v1/kernels.KernelsApiService/ListKernelSessionOutput":
                label = body.get("versionLabel", "")
                seen_labels.append(label)
                if label == "7":
                    # Stub-like: only a log -> must be REJECTED
                    return httpx.Response(200, json={"files": [], "log": "stub log here"})
                if label == "version-7":
                    # Correct cancelled-version snapshot
                    return httpx.Response(200, json={
                        "files": [{"url": "http://test/f/task_a_labeled_shard_0.jsonl",
                                   "fileName": "task_a_labeled_shard_0.jsonl"}],
                        "log": "real run log",
                        "nextPageToken": "",
                    })
                return httpx.Response(200, json={"files": [], "log": ""})
            return httpx.Response(404, json={"code": 404, "message": "nope"})

        code = self._run_fetch(handler, out_dir)
        self.assertEqual(code, 0)
        self.assertIn("7", seen_labels)          # plain spelling tried first
        self.assertIn("version-7", seen_labels)  # fallback spelling accepted
        # Regression guard: must hit api.kaggle.com, not www.kaggle.com
        for u in seen_urls:
            if u.startswith("http://test/"):
                continue
            self.assertIn("api.kaggle.com", u, f"URL routed to wrong host: {u}")
        saved_file = os.path.join(out_dir, "task_a_labeled_shard_0.jsonl")
        self.assertTrue(os.path.isfile(saved_file))
        self.assertTrue(os.path.isfile(os.path.join(out_dir, "my-slug.log")))
        with open(saved_file) as f:
            self.assertEqual(f.read(), "REALDATA")

    def test_2_all_versions_empty_exit_code_3(self):
        import httpx
        out_dir = tempfile.mkdtemp(prefix="vh_out2_")

        def handler(request):
            body = json.loads(request.content or b"{}")
            if request.url.path == "/v1/kernels.KernelsApiService/ListKernelSessionOutput":
                return httpx.Response(200, json={"files": [], "log": ""})
            return httpx.Response(404)

        code = self._run_fetch(handler, out_dir)
        self.assertEqual(code, 3)  # nothing recoverable - explicit signal
        self.assertEqual(os.listdir(out_dir), [])

    def test_3_meta_reads_current_version(self):
        import httpx
        import app.services.kaggle_versioned_output as helper

        def handler(request):
            if request.url.path == "/v1/kernels.KernelsApiService/ListKernels":
                return httpx.Response(200, json={"kernels": [
                    {"ref": "owner/other", "currentVersionNumber": 2},
                    {"ref": "owner/my-slug", "currentVersionNumber": 11},
                ]})
            return httpx.Response(404)

        real_client = httpx.Client
        helper.httpx.Client = staticmethod(lambda *a, **kw: real_client(
            *a, transport=httpx.MockTransport(handler), **kw))
        try:
            code = helper.main(["meta", "owner", "my-slug"])
        finally:
            helper.httpx.Client = real_client
        self.assertEqual(code, 0)

    def test_4_auth_header_sent(self):
        import httpx
        import app.services.kaggle_versioned_output as helper
        headers_seen = {}

        def handler(request):
            headers_seen.update(dict(request.headers))
            if request.url.path == "/v1/kernels.KernelsApiService/ListKernels":
                return httpx.Response(200, json={"kernels": [
                    {"ref": "owner/my-slug", "currentVersionNumber": 5}]})
            return httpx.Response(404)

        real_client = httpx.Client
        helper.httpx.Client = staticmethod(lambda *a, **kw: real_client(
            *a, transport=httpx.MockTransport(handler), **kw))
        try:
            helper.main(["meta", "owner", "my-slug"])
        finally:
            helper.httpx.Client = real_client
        self.assertEqual(headers_seen.get("authorization"), "Bearer test-token")


if __name__ == "__main__":
    unittest.main(verbosity=2)
