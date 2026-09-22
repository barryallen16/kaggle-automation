"""Bench/serve plain/draft pairs stay in sync (single source discipline).

The draft file is canonical for local runs (SPEC env flags); the plain file
is a frozen single-file Kaggle push snapshot. This test pins that contract:
- draft files accept the SPEC override and branch on it,
- plain files point at the draft as canonical and stay standalone
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


class TestBenchServeSync(unittest.TestCase):
    def test_bench_draft_parameterized(self):
        code = _read("bench_qwen38_draft.py")
        self.assertIn("BENCH_SPEC", code)
        self.assertIn("BENCH_VARIANT", code)
        self.assertIn("USE_SPEC", code)

    def test_serve_draft_parameterized(self):
        code = _read("serve_qwen3_8_draft.py")
        self.assertIn("SPEC_USE_DRAFT", code)
        self.assertIn("USE_SPEC", code)

    def test_plain_files_marked_frozen(self):
        for name in ("bench_qwen38_plain.py", "serve_qwen3_8_plain.py"):
            code = _read(name)
            self.assertIn("frozen single-file push snapshot", code)
            self.assertIn("canonical", code)

    def test_plain_files_standalone(self):
        # Each preset pushes exactly one file: plain snapshots must never
        # execute their draft sibling (that path does not exist on Kaggle).
        # Mentions inside comments/docstrings are fine.
        for name, sibling in (
            ("bench_qwen38_plain.py", "bench_qwen38_draft"),
            ("serve_qwen3_8_plain.py", "serve_qwen3_8_draft"),
        ):
            code_lines = []
            in_docstring = False
            for line in _read(name).splitlines():
                s = line.strip()
                if s.startswith('"""'):
                    in_docstring = not in_docstring
                    continue
                if in_docstring or s.startswith("#"):
                    continue
                code_lines.append(line)
            code_only = "\n".join(code_lines)
            self.assertNotIn(sibling, code_only)
            self.assertNotIn("runpy", code_only)

    def test_all_four_presets_available(self):
        import importlib
        import sys

        sys.path.insert(0, os.path.join(REPO, "app"))
        from routers import presets as _presets_mod

        presets = importlib.reload(_presets_mod)
        for key in (
            "serve-qwen38-plain",
            "serve-qwen38-draft",
            "bench-qwen38-plain",
            "bench-qwen38-draft",
        ):
            p = presets.PRESETS.get(key)
            self.assertIsNotNone(p, key)
            self.assertTrue((presets.PRESETS_DIR / p["file"]).is_file(), key)


if __name__ == "__main__":
    unittest.main(verbosity=2)
