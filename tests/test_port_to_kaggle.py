"""Hermetic tests for skills/port-to-kaggle/scripts/port_to_kaggle.py."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "skills", "port-to-kaggle", "scripts", "port_to_kaggle.py")

DATA_TMP = None


def setUpModule():
    global DATA_TMP
    DATA_TMP = tempfile.mkdtemp(prefix="port_harness_")
    os.environ["AUTOMATION_DATA_DIR"] = DATA_TMP


def tearDownModule():
    if DATA_TMP and os.path.isdir(DATA_TMP):
        shutil.rmtree(DATA_TMP, ignore_errors=True)
    os.environ.pop("AUTOMATION_DATA_DIR", None)


def run_port(src_text, in_name="in.py", extra=()):
    tmp = tempfile.mkdtemp(prefix="port_case_")
    inp = os.path.join(tmp, in_name)
    out = os.path.join(tmp, "out" + os.path.splitext(in_name)[1])
    rep = os.path.join(tmp, "report.json")
    with open(inp, "w", encoding="utf-8") as f:
        f.write(src_text)
    cmd = [sys.executable, SCRIPT, inp, "--out", out, "--report", rep, *extra]
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=REPO, check=False)
    report = {}
    if os.path.exists(rep):
        with open(rep, encoding="utf-8") as f:
            report = json.load(f)
    return r, tmp, out, report


class TestPortToKaggle(unittest.TestCase):
    def test_py_gets_fallback_buffering_dirs_and_compiles(self):
        src = "print('hello')\n"
        r, tmp, out, rep = run_port(src)
        try:
            self.assertEqual(r.returncode, 0, r.stderr)
            with open(out, encoding="utf-8") as f:
                out_src = f.read()
            compile(out_src, out, "exec")
            self.assertIn("_get_shard_var", out_src)
            self.assertIn("line_buffering", out_src)
            self.assertIn("WORKING_DIR", out_src)
            self.assertIn("shard-fallback-header", rep["fixes"])
            # idempotent: second run adds nothing
            _r2, tmp2, out2, rep2 = run_port(out_src)
            try:
                with open(out2, encoding="utf-8") as f:
                    twice = f.read()
                self.assertEqual(out_src, twice)
                self.assertEqual(rep2["fixes"], [])
            finally:
                shutil.rmtree(tmp2, ignore_errors=True)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_ipynb_missing_kernelspec_fixed(self):
        nb = {"cells": [{"cell_type": "code", "execution_count": None, "metadata": {}, "outputs": [], "source": ["print(1)"]}], "metadata": {}, "nbformat": 4, "nbformat_minor": 2}
        r, tmp, out, _rep = run_port(json.dumps(nb), "in.ipynb")
        try:
            self.assertEqual(r.returncode, 0, r.stderr)
            with open(out, encoding="utf-8") as f:
                nb_out = json.load(f)
            self.assertEqual(nb_out["metadata"]["kernelspec"]["name"], "python3")
            tags = [t for c in nb_out["cells"] for t in (c.get("metadata") or {}).get("tags", [])]
            self.assertIn("kaggle-automation-shard-fallback", tags)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_warnings_and_check_mode(self):
        src = "import torch\nprint('x')\nOUTPUT_FILE='/kaggle/working/out.jsonl'\n"
        r, tmp, out, rep = run_port(
            src, extra=["--title", "T" * 60, "--accelerator", "nvidia-tesla-t4-x2", "--check"]
        )
        try:
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertFalse(os.path.exists(out))  # --check writes nothing
            joined = "\n".join(rep["warnings"])
            self.assertIn("title>", joined)
            self.assertIn("GPU guard", joined)
            self.assertIn("SHARD_ID", joined)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
