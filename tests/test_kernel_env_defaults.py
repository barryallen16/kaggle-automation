"""Kernel env injection: HF_TOKEN/WANDB_* flow through, ZROK_* never does."""

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
    def test_hf_token_injected_when_set(self):
        cfg = _config()
        with mock.patch.dict(os.environ, {"HF_TOKEN": "hf_x"}, clear=False):
            env = cfg.get_kernel_env_defaults()
        self.assertEqual(env.get("HF_TOKEN"), "hf_x")

    def test_zrok_keys_never_injected(self):
        # The zrok public share was removed: publishing a public endpoint out
        # of a Kaggle kernel breaks the Kaggle AUP and gets accounts
        # suspended. Even if the env still holds a token, do not inject it.
        cfg = _config()
        with mock.patch.dict(
            os.environ, {"ZROK_TOKEN": "tok123", "ZROK_NAME": "llama"}, clear=False
        ):
            env = cfg.get_kernel_env_defaults()
        self.assertNotIn("ZROK_TOKEN", env)
        self.assertNotIn("ZROK_NAME", env)

    def test_hf_token_absent_when_unset(self):
        cfg = _config()
        env = dict(os.environ)
        env.pop("HF_TOKEN", None)
        with mock.patch.dict(os.environ, env, clear=True):
            defaults = cfg.get_kernel_env_defaults()
        self.assertNotIn("HF_TOKEN", defaults)


if __name__ == "__main__":
    unittest.main(verbosity=2)
