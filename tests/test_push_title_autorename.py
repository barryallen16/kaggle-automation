"""Second GPU session with a taken title launches as Title (2)."""

import os
import sys
import unittest

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TEST_DIR)


def _ks():
    import importlib

    import services.kaggle_service as ks

    return importlib.reload(ks)


class TestResolveFreeTitle(unittest.TestCase):
    def test_free_title_unchanged(self):
        ks = _ks()
        self.assertEqual(
            ks.KaggleService._resolve_free_title("acc", "Bench eval", set()), "Bench eval"
        )

    def test_taken_title_gets_suffix(self):
        ks = _ks()
        slug = ks.KaggleService.sanitize_slug("Bench eval")
        free = ks.KaggleService._resolve_free_title("acc", "Bench eval", {f"acc/{slug}"})
        self.assertEqual(free, "Bench eval (2)")

    def test_chain_takes_next_free_number(self):
        ks = _ks()
        s = ks.KaggleService.sanitize_slug
        taken = {f"acc/{s('Bench eval')}", f"acc/{s('Bench eval (2)')}"}
        self.assertEqual(
            ks.KaggleService._resolve_free_title("acc", "Bench eval", taken),
            "Bench eval (3)",
        )

    def test_long_title_still_fits_50_chars(self):
        ks = _ks()
        long_title = "B" * 50
        slug = ks.KaggleService.sanitize_slug(long_title)
        free = ks.KaggleService._resolve_free_title("acc", long_title, {f"acc/{slug}"})
        self.assertLessEqual(len(free), 50)
        self.assertTrue(free.endswith(" (2)"))

    def test_exhausted_returns_none(self):
        ks = _ks()
        s = ks.KaggleService.sanitize_slug
        taken = {f"acc/{s('T')}", f"acc/{s('T (2)')}"}
        self.assertIsNone(
            ks.KaggleService._resolve_free_title("acc", "T", taken, max_attempts=1)
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
