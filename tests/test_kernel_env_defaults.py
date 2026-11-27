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

    def test_build_env_preamble_notebook_has_single_env_cell(self):
        import json

        from services.kaggle_kernel_identity import build_env_preamble

        raw = build_env_preamble({"HF_TOKEN": "hf_abc", "MAX_RUNTIME_MINUTES": "120"}, True)
        nb = json.loads(raw)
        self.assertEqual(len(nb["cells"]), 1)
        self.assertEqual(nb["cells"][0]["metadata"].get("tags"), ["kaggle-automation-env"])
        src = "".join(nb["cells"][0]["source"])
        self.assertEqual(src.count("os.environ['HF_TOKEN']"), 1)
        self.assertEqual(src.count("os.environ['MAX_RUNTIME_MINUTES']"), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
