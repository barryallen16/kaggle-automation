"""ZROK_* keys flow into pushed kernels like HF_TOKEN (serve presets need them)."""

import importlib
import os
import sys
import unittest
from unittest import mock

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TEST_DIR)


def _config():
    sys.path.insert(0, os.path.join(os.path.dirname(TEST_DIR), "app"))
    import config as cfg

    return importlib.reload(cfg)


class TestKernelEnvDefaults(unittest.TestCase):
    def test_zrok_token_injected_when_set(self):
        cfg = _config()
        with mock.patch.dict(
            os.environ, {"ZROK_TOKEN": "tok123", "ZROK_NAME": "llama"}, clear=False
        ):
            env = cfg.get_kernel_env_defaults()
        self.assertEqual(env.get("ZROK_TOKEN"), "tok123")
        self.assertEqual(env.get("ZROK_NAME"), "llama")

    def test_zrok_keys_absent_when_unset(self):
        cfg = _config()
        env = dict(os.environ)
        env.pop("ZROK_TOKEN", None)
        env.pop("ZROK_NAME", None)
        with mock.patch.dict(os.environ, env, clear=True):
            defaults = cfg.get_kernel_env_defaults()
        self.assertNotIn("ZROK_TOKEN", defaults)
        self.assertNotIn("ZROK_NAME", defaults)


if __name__ == "__main__":
    unittest.main(verbosity=2)
