"""Bench plain/draft pair stays in sync (single source discipline).

The draft file is canonical for local runs (SPEC env flags); the plain file
is a frozen single-file Kaggle push snapshot. This test pins that contract:
- the draft file accepts the SPEC override and branches on it,
- the plain file points at the draft as canonical and stays standalone
  (no sibling imports - each preset pushes exactly one file).
"""

import os
import unittest

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(TEST_DIR)
BENCH = os.path.join(REPO, "benchmarks")


def _read(name):
    with open(os.path.join(BENCH, name), encoding="utf-8") as f:
        return f.read()


class TestBenchSync(unittest.TestCase):
    def test_bench_draft_parameterized(self):
        code = _read("bench_qwen38_draft.py")
        self.assertIn("BENCH_SPEC", code)
        self.assertIn("BENCH_VARIANT", code)
        self.assertIn("USE_SPEC", code)

    def test_plain_file_marked_frozen(self):
        code = _read("bench_qwen38_plain.py")
        self.assertIn("frozen single-file push snapshot", code)
        self.assertIn("canonical", code)

    def test_plain_file_standalone(self):
        # Each preset pushes exactly one file: the plain snapshot must never
        # execute its draft sibling (that path does not exist on Kaggle).
        # Mentions inside comments/docstrings are fine.
        code_lines = []
        in_docstring = False
        for line in _read("bench_qwen38_plain.py").splitlines():
            s = line.strip()
            if s.startswith('"""'):
                in_docstring = not in_docstring
                continue
            if in_docstring or s.startswith("#"):
                continue
            code_lines.append(line)
        code_only = "\n".join(code_lines)
        self.assertNotIn("bench_qwen38_draft", code_only)
        self.assertNotIn("runpy", code_only)

    def test_bench_presets_available(self):
        import importlib
        import sys

        sys.path.insert(0, os.path.join(REPO, "app"))
        from routers import presets as _presets_mod

        presets = importlib.reload(_presets_mod)
        for key in ("bench-qwen38-plain", "bench-qwen38-draft"):
            p = presets.PRESETS.get(key)
            self.assertIsNotNone(p, key)
            self.assertTrue((presets.PRESETS_DIR / p["file"]).is_file(), key)

    def test_serve_presets_are_gone(self):
        # The serve presets published a public zrok share out of the kernel,
        # which breaks the Kaggle AUP. They must not come back.
        import importlib
        import sys

        sys.path.insert(0, os.path.join(REPO, "app"))
        from routers import presets as _presets_mod

        presets = importlib.reload(_presets_mod)
        self.assertFalse([k for k in presets.PRESETS if k.startswith("serve-")])
        for name in ("serve_qwen3_8_plain.py", "serve_qwen3_8_draft.py"):
            self.assertFalse(os.path.exists(os.path.join(BENCH, name)), name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
