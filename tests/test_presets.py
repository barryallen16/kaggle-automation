"""Single-run script presets served from benchmarks/ (fixed key map, no traversal)."""

import os
import sys
import unittest

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(TEST_DIR)
sys.path.insert(0, os.path.join(REPO, "app"))


def _presets():
    import importlib

    from fastapi import HTTPException
    from routers import presets as _presets_mod

    return importlib.reload(_presets_mod), HTTPException


def call(coro):
    import asyncio

    return asyncio.run(coro)


class TestPresetListing(unittest.TestCase):
    def test_plain_listed_and_recommended(self):
        presets, _ = _presets()
        data = call(presets.list_presets())
        self.assertTrue(data["success"])
        by_key = {p["key"]: p for p in data["presets"]}
        self.assertIn("serve-qwen38-plain", by_key)
        self.assertTrue(by_key["serve-qwen38-plain"]["recommended"])
        self.assertTrue(by_key["serve-qwen38-plain"]["available"])
        self.assertEqual(by_key["serve-qwen38-plain"]["title"], "serve-qwen38-plain")
        self.assertEqual(
            by_key["serve-qwen38-plain"]["accelerator"], "nvidia-tesla-t4-x2"
        )
        # serve scripts come before benchmarks; benchmarks are not recommended
        keys = [p["key"] for p in data["presets"]]
        self.assertLess(
            keys.index("serve-qwen38-plain"), keys.index("bench-qwen38-plain")
        )
        self.assertFalse(by_key["bench-qwen38-plain"]["recommended"])

    def test_preset_content_matches_file(self):
        presets, _ = _presets()
        data = call(presets.get_preset("bench-qwen38-plain"))
        self.assertTrue(data["success"])
        on_disk = (
            presets.PRESETS_DIR / presets.PRESETS["bench-qwen38-plain"]["file"]
        ).read_text(encoding="utf-8")
        self.assertEqual(data["code"], on_disk)
        self.assertIn("VARIANT", data["code"])

    def test_serve_plain_recommended_draft_not(self):
        presets, _ = _presets()
        data = call(presets.list_presets())
        by_key = {p["key"]: p for p in data["presets"]}
        for key in ("serve-qwen38-plain", "serve-qwen38-draft"):
            self.assertIn(key, by_key)
            self.assertTrue(by_key[key]["available"])
            self.assertIn("stays up", by_key[key]["label"])
        self.assertTrue(by_key["serve-qwen38-plain"]["recommended"])
        self.assertFalse(by_key["serve-qwen38-draft"]["recommended"])

    def test_serve_plain_content_matches_file(self):
        presets, _ = _presets()
        data = call(presets.get_preset("serve-qwen38-plain"))
        self.assertTrue(data["success"])
        on_disk = (
            presets.PRESETS_DIR / presets.PRESETS["serve-qwen38-plain"]["file"]
        ).read_text(encoding="utf-8")
        self.assertEqual(data["code"], on_disk)
        self.assertIn("ZROK_TOKEN", data["code"])
        self.assertNotIn("u90GC1utceEh", data["code"])

    def test_unknown_key_404s(self):
        presets, HTTPException = _presets()
        with self.assertRaises(HTTPException) as ctx:
            call(presets.get_preset("nope"))
        self.assertEqual(ctx.exception.status_code, 404)

    def test_traversal_key_404s(self):
        presets, HTTPException = _presets()
        with self.assertRaises(HTTPException) as ctx:
            call(presets.get_preset("../secret"))
        self.assertEqual(ctx.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
