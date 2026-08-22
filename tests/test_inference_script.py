"""Local validation harness for kaggle_batch_inference_task_a.py.

Executes the real script end-to-end with mocked torch/transformers/qwen_vl_utils/PIL,
a patched subprocess (installs/wget recorded, not run), REAL requests against a local
HTTP image server, and REAL file I/O - so slicing, resume, checkpointing, output
format, install flags and env-var coercion are all genuinely exercised.
"""
import contextlib
import http.server
import json
import os
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from typing import ClassVar

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT_PATH = os.path.join(REPO, "kaggle_batch_inference_task_a.py")
with open(SCRIPT_PATH, encoding="utf-8") as f:
    SRC = f.read()

PNG_BYTES = b"\x89PNG\r\n\x1a\nfakeimagebytes"

# ---------------------------------------------------------------- HTTP server
class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/ok/"):
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.end_headers()
            self.wfile.write(PNG_BYTES)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *a):
        pass

_SERVER = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Handler)
PORT = _SERVER.server_address[1]
threading.Thread(target=_SERVER.serve_forever, daemon=True).start()

OK_URL = f"http://127.0.0.1:{PORT}/ok/img.jpg"
BAD_URL = f"http://127.0.0.1:{PORT}/missing.jpg"

# ------------------------------------------------------------ fake ML stack
STATE = {
    "gpu_count": 2,
    "responses": [],       # popped once per batch_decode call
    "commands": [],        # recorded subprocess commands
    "gen_tail": [91, 92],  # token tail appended by FakeModel.generate
}

class FakeBatchFeature(dict):
    """dict-backed BatchFeature: **unpacking AND attribute access both work."""
    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError:
            raise AttributeError(k)
    def to(self, dev):
        return self

class FakeImage:
    def __init__(self):
        self.size = (800, 600)
    def convert(self, mode):
        return self
    def thumbnail(self, size):
        pass

def _build_fakes():
    torch_mod = types.ModuleType("torch")
    torch_mod.float16 = "fp16"
    class _NoGrad:
        def __enter__(self): return self
        def __exit__(self, *a): return False
    torch_mod.no_grad = lambda: _NoGrad()
    props = types.SimpleNamespace(total_memory=15 * 1024**3)
    torch_mod.cuda = types.SimpleNamespace(
        device_count=lambda: STATE["gpu_count"],
        get_device_name=lambda i: f"FakeGPU{i}",
        get_device_properties=lambda i: props,
    )
    sys.modules["torch"] = torch_mod

    tr = types.ModuleType("transformers")

    class FakeModel:
        device = "cpu"
        loaded_with: ClassVar[dict] = {}
        @classmethod
        def from_pretrained(cls, model_id, **kw):
            cls.loaded_with = {"model_id": model_id, **kw}
            return cls()
        def generate(self, input_ids=None, **kw):
            tail = list(STATE.get("gen_tail", [91, 92]))
            return [row + tail for row in input_ids]
    tr.AutoModelForImageTextToText = FakeModel

    class FakeProcessor:
        @classmethod
        def from_pretrained(cls, model_id, **kw):
            inst = cls()
            inst.loaded_id = model_id
            return inst
        def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=True):
            return "<chat>"
        def __call__(self, text=None, images=None, videos=None, padding=True, return_tensors="pt"):
            return FakeBatchFeature({
                "input_ids": [[11, 12, 13]],
                "attention_mask": [[1, 1, 1]],
            })
        def batch_decode(self, ids, skip_special_tokens=True, clean_up_tokenization_spaces=False):
            return [STATE["responses"].pop(0)]
    tr.AutoProcessor = FakeProcessor
    tr.BitsAndBytesConfig = lambda **kw: types.SimpleNamespace(bnb=True)
    tr._last_model = FakeModel
    sys.modules["transformers"] = tr

    ql = types.ModuleType("qwen_vl_utils")
    def process_vision_info(messages):
        n = sum(1 for c in messages[0]["content"] if c.get("type") == "image")
        return ([object()] * n, None)
    ql.process_vision_info = process_vision_info
    sys.modules["qwen_vl_utils"] = ql

    pil_pkg = types.ModuleType("PIL")
    pil_img = types.ModuleType("PIL.Image")  # `from PIL import Image` yields THIS module
    pil_img.open = lambda bio: FakeImage()
    pil_img.Image = FakeImage  # real Pillow exposes PIL.Image.Image (used in annotations)
    pil_pkg.Image = pil_img
    sys.modules["PIL"] = pil_pkg
    sys.modules["PIL.Image"] = pil_img
    return tr

def _teardown_fakes():
    for m in ("torch", "transformers", "qwen_vl_utils", "PIL", "PIL.Image"):
        sys.modules.pop(m, None)

_orig_run = subprocess.run

def _fake_run(cmd=None, *a, **k):
    STATE["commands"].append(cmd if isinstance(cmd, str) else " ".join(map(str, cmd or [])))
    return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

@contextlib.contextmanager
def sandbox(dataset_items, extra_ns=None, gpu_count=2, responses=None, env_overrides=None):
    """Isolated CWD + fakes + patched subprocess; yields the exec namespace."""
    tmp = tempfile.mkdtemp(prefix="task_a_harness_")
    with open(os.path.join(tmp, "task_a_dataset.jsonl"), "w", encoding="utf-8") as f:
        f.writelines(json.dumps(item) + "\n" for item in dataset_items)

    STATE["gpu_count"] = gpu_count
    STATE["responses"] = list(responses or [])
    STATE["commands"] = []
    tr_mod = _build_fakes()
    subprocess.run = _fake_run

    saved_cwd = os.getcwd()
    saved_environ = {k: os.environ.get(k) for k in ("START_INDEX", "END_INDEX", "SHARD_ID", "TOTAL_SHARDS")}
    for k in ("START_INDEX", "END_INDEX", "SHARD_ID", "TOTAL_SHARDS"):
        os.environ.pop(k, None)
    for k, v in (env_overrides or {}).items():
        os.environ[k] = v
    os.chdir(tmp)

    ns = {"__name__": "__main__"}
    if extra_ns:
        ns.update(extra_ns)
    try:
        exec(  # noqa: S102 - the harness intentionally executes the notebook under test
            compile(SRC, SCRIPT_PATH, "exec"), ns
        )
        # NOTE: yield INSIDE try - the with-body must run while tmp/ fakes are live;
        # cleanup happens only after the body finishes (or raises).
        ns["_transformers"] = tr_mod
        ns["_tmp_dir"] = tmp
        yield ns
    finally:
        os.chdir(saved_cwd)
        subprocess.run = _orig_run
        for k, v in saved_environ.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        _teardown_fakes()
        shutil.rmtree(tmp, ignore_errors=True)

# ------------------------------------------------------------------ fixtures
def make_items(n, good_predicate=lambda i: i % 2 == 0 or i == 5):
    """n items; failing-image ids chosen so B-test gets 4 good / 2 bad for n=6."""
    items = []
    for i in range(n):
        url = OK_URL if good_predicate(i) else BAD_URL
        items.append({
            "id": f"id{i}",
            "url": f"https://shop.example/p/{i}",
            "images": [url, url],
            "prompt": f"Describe outfit {i} as JSON.",
            "grounding_metadata": {"cat": f"c{i}"},
        })
    return items

RESPONSES_B = [
    '{"fit":"casual","colors":["blue","white"]}',          # thinking disabled -> direct JSON
    'Sure! {"style":"sporty"} hope it helps',
    '<think>hmm</think>{"nested":{"deep":1},"top":2}',     # stray think block stripped
    'no json here at all',
]

# ================================================================== tests ==
class TestScript(unittest.TestCase):
    def test_A_compiles_and_install_flags(self):
        compile(SRC, SCRIPT_PATH, "exec")  # syntax gate
        items = make_items(0)
        with sandbox(items, responses=[], gpu_count=2) as _ns:
            cmds = "\n".join(STATE["commands"])
            self.assertIn("--no-deps accelerate bitsandbytes qwen-vl-utils", cmds)
            self.assertIn("--no-deps git+https://github.com/huggingface/transformers.git", cmds)
            self.assertIn("tokenizers>=0.23.1", cmds)
            # Dataset downloads with explicit -O into scratch (never CWD/output)
            self.assertIn("wget -q -O", cmds)
            self.assertIn("https://huggingface.co/datasets/barryallen16/fitcheck-annotate-dataset/resolve/main/task_a_dataset.jsonl", cmds)
            # Regression guard: dataset must NOT land in /kaggle/working output
            self.assertNotIn("cd /kaggle/working", cmds)
            self.assertIn("task_a_labeled_prior.jsonl", cmds)
        # uv path must carry --system (Kaggle has no active venv); pip path stays quiet
        self.assertIn("uv pip install --system", cmds)
        self.assertNotIn("pip install --system", cmds.replace("uv pip install --system", ""))

    def test_model_class_is_image_text_to_text(self):
        # Regression guard for the critical fix: must not USE AutoModelForCausalLM.
        # (A comment mentioning the name is fine; a from_pretrained call is not.)
        self.assertNotIn("AutoModelForCausalLM.from_pretrained", SRC)
        self.assertIn("AutoModelForImageTextToText.from_pretrained", SRC)

    def test_B_single_shard_end_to_end(self):
        items = make_items(6)  # ids 1,3 -> bad URLs
        with sandbox(items, responses=RESPONSES_B, gpu_count=2) as ns:
            # id1/id3 fail on image download; id5 emits unparseable text and is
            # SKIPPED by the fine-tuning guard instead of being written raw.
            self.assertEqual(ns["success_count"], 3)
            self.assertEqual(ns["failed_count"], 3)
            self.assertEqual(len(ns["items_to_process"]), 6)

            out_path = ns["OUTPUT_FILE"]
            self.assertTrue(os.path.exists(out_path))
            with open(out_path, encoding="utf-8") as f:
                rows = [json.loads(l) for l in f if l.strip()]
            self.assertEqual(len(rows), 3)
            self.assertEqual([r["id"] for r in rows], ["id0", "id2", "id4"])

            # teacher_output parsing matrix (clean JSON only - no raw_output row:
            # unparseable generations must never enter the fine-tuning dataset)
            self.assertEqual(rows[0]["teacher_output"], {"fit": "casual", "colors": ["blue", "white"]})
            self.assertEqual(rows[1]["teacher_output"], {"style": "sporty"})
            self.assertEqual(rows[2]["teacher_output"], {"nested": {"deep": 1}, "top": 2})
            # enable_thinking=False must be requested on the chat template
            self.assertIn("enable_thinking=False", SRC)

            for r in rows:
                self.assertEqual(r["grounding_metadata"]["cat"], "c" + r["id"][2:])
                self.assertTrue(r["url"].startswith("https://shop.example"))

            # model class actually exercised
            self.assertEqual(ns["_transformers"]._last_model.loaded_with["model_id"], "Qwen/Qwen3.6-27B")
            self.assertTrue(ns["_transformers"]._last_model.loaded_with["quantization_config"].bnb)
            # keep rows alive post-sandbox for optional debugging
            globals()["_LAST_ROWS_B"] = rows

    def test_C_resume_skips_processed(self):
        items = make_items(6)
        # Persistent dir across TWO execs (sandbox() wipes its tmp, so manage manually)
        tmp = tempfile.mkdtemp(prefix="task_a_resume_")
        try:
            with open(os.path.join(tmp, "task_a_dataset.jsonl"), "w", encoding="utf-8") as f:
                f.writelines(json.dumps(item) + "\n" for item in items)

            STATE.update({"gpu_count": 2, "responses": list(RESPONSES_B), "commands": []})
            _build_fakes(); subprocess.run = _fake_run
            saved_cwd = os.getcwd(); os.chdir(tmp)
            try:
                ns = {"__name__": "__main__"}
                exec(  # noqa: S102 - the harness intentionally executes the notebook under test
                    compile(SRC, SCRIPT_PATH, "exec"), ns
                )
                out_path = ns["OUTPUT_FILE"]
                # 3 clean rows; id5's unparseable output was skipped, not written
                with open(out_path, encoding="utf-8") as f:
                    self.assertEqual(len(f.readlines()), 3)

                # second execution in SAME dir -> everything already done except
                # failures. id1/id3 fail on images again; id5 is retried and its
                # fresh (still unparseable) response is skipped by the guard.
                STATE["responses"] = ["also no json"]
                ns2 = {"__name__": "__main__"}
                exec(  # noqa: S102 - the harness intentionally executes the notebook under test
                    compile(SRC, SCRIPT_PATH, "exec"), ns2
                )
                self.assertEqual(ns2["success_count"], 0)
                self.assertEqual(ns2["failed_count"], 3)
                with open(ns2["OUTPUT_FILE"], encoding="utf-8") as f:
                    self.assertEqual(len(f.readlines()), 3)
            finally:
                os.chdir(saved_cwd)
                subprocess.run = _orig_run
                _teardown_fakes()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_D_multi_shard_partition_exact(self):
        items = make_items(5, good_predicate=lambda i: True)
        responses = [f'{{"ok":{i}}}' for i in range(5)]
        seen = {}
        for shard in range(3):
            with sandbox(items, responses=list(responses), gpu_count=2,
                         extra_ns={"SHARD_ID": shard, "TOTAL_SHARDS": 3}) as ns:
                self.assertEqual(ns["success_count"], len(ns["shard_items"]))
                with open(ns["OUTPUT_FILE"], encoding="utf-8") as f:
                    out_rows = [json.loads(l) for l in f if l.strip()]
                seen[shard] = [r["id"] for r in out_rows]

        self.assertEqual(seen[0], ["id0", "id1"])
        self.assertEqual(seen[1], ["id2", "id3"])
        self.assertEqual(seen[2], ["id4"])
        union: list = []
        for shard in range(3):
            union.extend(seen[shard])
        self.assertEqual(sorted(union), [f"id{i}" for i in range(5)])
        self.assertEqual(len(union), len(set(union)))  # zero overlap

    def test_E_env_vars_coerced_to_int(self):
        # Regression: pre-fix, env strings reached slicing -> TypeError crash
        items = make_items(6, good_predicate=lambda i: True)
        with sandbox(items,
                     responses=['{"v":1}', '{"v":2}', '{"v":3}'],
                     gpu_count=2,
                     env_overrides={"START_INDEX": "2", "END_INDEX": "5"}) as ns:
            self.assertIsInstance(ns["START_INDEX"], int)
            self.assertIsInstance(ns["END_INDEX"], int)
            self.assertEqual([i["id"] for i in ns["shard_items"]], ["id2", "id3", "id4"])
            self.assertEqual(ns["success_count"], 3)

    def test_F_extract_json_response_cases(self):
        items = make_items(0)
        with sandbox(items, responses=[], gpu_count=1) as ns:
            fn = ns["extract_json_response"]
            self.assertEqual(fn('```json\n{"a":1}\n```'), {"a": 1})
            self.assertEqual(fn('text ```\n{"b":2}\n``` tail'), {"b": 2})
            self.assertEqual(fn('prefix {"nested":{"x":1}} suffix'), {"nested": {"x": 1}})
            self.assertEqual(fn('{"a":1}'), {"a": 1})
            self.assertEqual(fn('utter garbage'), {"raw_output": "utter garbage"})
            self.assertEqual(fn('   '), {"raw_output": ""})

    def test_G_no_gpu_exits(self):
        tmp = tempfile.mkdtemp(prefix="task_a_nogpu_")
        try:
            open(os.path.join(tmp, "task_a_dataset.jsonl"), "w").close()
            STATE["gpu_count"] = 0
            _build_fakes(); subprocess.run = _fake_run
            saved = os.getcwd(); os.chdir(tmp)
            try:
                with self.assertRaises(SystemExit) as cm:
                    exec(  # noqa: S102 - the harness intentionally executes the notebook under test
                        compile(SRC, SCRIPT_PATH, "exec"), {"__name__": "__main__"}
                    )
                self.assertEqual(cm.exception.code, 1)
            finally:
                os.chdir(saved); subprocess.run = _orig_run; _teardown_fakes()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_H_truncated_generation_is_skipped(self):
        # Regression guard for the fine-tuning dataset: a generation that hits
        # MAX_NEW_TOKENS must NOT be written even if its text parses as JSON -
        # truncated labels would poison student-model training data.
        items = make_items(2, good_predicate=lambda i: True)
        old_tail = STATE["gen_tail"]
        try:
            STATE.update({
                "gen_tail": [7] * (384 + 50),  # completion length > MAX_NEW_TOKENS
                "commands": [],
            })
            with sandbox(items, responses=['{"fit":"x"}', '{"fit":"y"}'], gpu_count=2) as ns:
                self.assertEqual(ns["success_count"], 0)
                self.assertEqual(ns["failed_count"], 2)
                content = ""
                if os.path.exists(ns["OUTPUT_FILE"]):
                    with open(ns["OUTPUT_FILE"], encoding="utf-8") as f:
                        content = f.read()
                self.assertFalse(content.strip())
        finally:
            STATE["gen_tail"] = old_tail

if __name__ == "__main__":
    unittest.main(verbosity=2)
