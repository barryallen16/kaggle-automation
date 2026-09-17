"""Faithful benchmark: speculative Qwen3.8-27B serve (draft-dflash).

Mirrors serve_qwen3_8_w_draftmodel.py serve path exactly: binary v0.4.1,
Q4_K_XL main + DFlash2 Q8_0 draft, --spec-type draft-dflash,
--spec-draft-n-max 8, -b 1024 / -ub 512 / -t 4 / --parallel 1.
Zrok block removed (localhost bench).

Run (Single Run tab): T4 GPU x2, internet ON, full run (downloads ~44GB).
Paste back the ===BENCHMARK_JSON_START=== block.
"""

import sys as _ka_sys

try:
    _ka_sys.stdout.reconfigure(line_buffering=True)
    _ka_sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass

import os as _ka_os2

WORKING_DIR = (
    "/kaggle/working" if _ka_os2.path.exists("/kaggle/working") else _ka_os2.getcwd()
)
SCRATCH_DIR = "/kaggle/tmp" if _ka_os2.path.exists("/kaggle/tmp") else _ka_os2.getcwd()

import os as _ka_os


def _get_shard_var(name, default=None, cast=None):
    val = globals().get(name)
    if val is not None:
        return val
    env_val = _ka_os.environ.get(name)
    if env_val is not None and str(env_val).strip():
        if cast is not None:
            try:
                return cast(env_val)
            except (TypeError, ValueError):
                return default
        if default is not None:
            try:
                return type(default)(env_val)
            except (TypeError, ValueError):
                return default
        return env_val
    return default


SHARD_ID = _get_shard_var("SHARD_ID", 0, cast=int)
TOTAL_SHARDS = _get_shard_var("TOTAL_SHARDS", 1, cast=int)
START_INDEX = _get_shard_var("START_INDEX", None, cast=int)
END_INDEX = _get_shard_var("END_INDEX", None, cast=int)
TOTAL_ITEMS = _get_shard_var("TOTAL_ITEMS", None, cast=int)
SHARD_PARAMS = {}

import glob
import json
import os
import subprocess
import sys
import time
import urllib.request

VARIANT = "draft"
CONFIG = {
    "MODEL_URL": os.environ.get("MODEL_URL")
    or "https://huggingface.co/unsloth/Qwen3.8-27B-GGUF/resolve/main/Qwen3.8-27B-UD-Q4_K_XL.gguf",
    "MODEL_PATH": os.environ.get("MODEL_PATH")
    or os.path.join(SCRATCH_DIR, "qwen3.8-27b-q4.gguf"),
    "DRAFT_MODEL_URL": os.environ.get("DRAFT_MODEL_URL")
    or "https://huggingface.co/z-lab/Qwen3.8-27B-DFlash2-GGUF/resolve/main/Qwen3.8-27B-DFlash2-Q8_0.gguf",
    "DRAFT_MODEL_PATH": os.environ.get("DRAFT_MODEL_PATH")
    or os.path.join(SCRATCH_DIR, "qwen3.8-27b-dflash-q8.gguf"),
    "BINARY_URL": os.environ.get("BINARY_URL")
    or "https://github.com/ai-dock/llama.cpp-cuda/releases/download/v0.4.1/llama.cpp-v0.4.1-cuda-12.8-amd64.tar.gz",
    "BIN_DIR": os.environ.get("BIN_DIR") or os.path.join(SCRATCH_DIR, "llama_bin"),
    "PORT": int(os.environ.get("PORT") or 8080),
    "API_KEY": os.environ.get("LLM_API_KEY") or "kaggle-opencode-key",
    "CONTEXT_SIZE": int(os.environ.get("CONTEXT_SIZE") or 131072),
    "LOG_SERVER": os.environ.get("LOG_SERVER")
    or os.path.join(SCRATCH_DIR, "llama_server.log"),
}

PROMPTS = [
    {"name": "warmup", "content": "hi", "max_tokens": 8},
    {
        "name": "essay",
        "content": "Write a 5-paragraph essay on the history and future of quantum computing, including its impact on cryptography and materials science.",
        "max_tokens": 256,
    },
    {
        "name": "code",
        "content": "Write a Python function that implements LRU cache with O(1) get and put, with type hints and a short usage example.",
        "max_tokens": 256,
    },
]


def log(stage, msg):
    print(f"[{stage}] {msg}", flush=True)


def run_cmd(cmd, check=True, env=None):
    r = subprocess.run(cmd, text=True, capture_output=True, env=env, check=False)
    if check and r.returncode != 0:
        raise RuntimeError(
            f"Command failed ({r.returncode}): {' '.join(cmd)}\n{(r.stdout or '') + (r.stderr or '')}"
        )
    return r


def gpu_guard():
    try:
        import torch

        n = torch.cuda.device_count()
        log("GPU", f"torch.cuda.device_count()={n}", flush=True)
        if n == 0:
            log("GPU", "FATAL: no CUDA device visible", flush=True)
            sys.exit(1)
        return n
    except ImportError:
        log("GPU", "torch missing, falling back to nvidia-smi", flush=True)
        r = run_cmd(["nvidia-smi", "-L"], check=False)
        log("GPU", (r.stdout or "").strip() or "nvidia-smi empty", flush=True)
        if r.returncode != 0:
            sys.exit(1)
        return 1


def ensure_assets():
    os.makedirs(SCRATCH_DIR, exist_ok=True)
    os.makedirs(CONFIG["BIN_DIR"], exist_ok=True)
    log("SETUP", "Installing aria2...", flush=True)
    subprocess.run(
        "apt-get update -qq && apt-get install -y -qq aria2", shell=True, check=True
    )
    for label, url_key, path_key in (
        ("model", "MODEL_URL", "MODEL_PATH"),
        ("draft", "DRAFT_MODEL_URL", "DRAFT_MODEL_PATH"),
    ):
        if not os.path.exists(CONFIG[path_key]):
            log("DOWNLOAD", f"Downloading {label} to {CONFIG[path_key]}...", flush=True)
            run_cmd(
                [
                    "aria2c",
                    "-x",
                    "16",
                    "-s",
                    "16",
                    "-k",
                    "1M",
                    "-d",
                    SCRATCH_DIR,
                    "-o",
                    os.path.basename(CONFIG[path_key]),
                    CONFIG[url_key],
                ]
            )
        else:
            log("DOWNLOAD", f"{label} present, skipping.", flush=True)
    server_bin = os.path.join(CONFIG["BIN_DIR"], "llama-server")
    if not os.path.exists(server_bin):
        log("DOWNLOAD", "Fetching CUDA prebuilt binary (v0.4.1)...", flush=True)
        tar_path = os.path.join(SCRATCH_DIR, "llama.tar.gz")
        run_cmd(["wget", "-q", CONFIG["BINARY_URL"], "-O", tar_path])
        run_cmd(
            ["tar", "-xzf", tar_path, "-C", CONFIG["BIN_DIR"], "--strip-components=1"],
            check=False,
        )
        cands = glob.glob(f"{CONFIG['BIN_DIR']}/**/llama-server", recursive=True)
        if not cands:
            raise FileNotFoundError("llama-server not found in archive")
        server_bin = cands[0]
        os.chmod(server_bin, 0o755)
        help_out = run_cmd([server_bin, "--help"], check=False)
        if "draft-dflash" not in ((help_out.stdout or "") + (help_out.stderr or "")):
            raise RuntimeError("binary lacks draft-dflash support, rebuild required")
    log("SETUP", f"Using server binary at: {server_bin}", flush=True)
    return server_bin


def start_server(server_bin):
    subprocess.run(
        ["pkill", "-9", "-f", "llama-server"], stderr=subprocess.DEVNULL, check=False
    )
    time.sleep(2)
    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = f"{CONFIG['BIN_DIR']}:/usr/local/cuda/lib64:" + env.get(
        "LD_LIBRARY_PATH", ""
    )
    env["CUDA_VISIBLE_DEVICES"] = "0,1"
    server_cmd = [
        server_bin,
        "-m",
        CONFIG["MODEL_PATH"],
        "-md",
        CONFIG["DRAFT_MODEL_PATH"],
        "--spec-type",
        "draft-dflash",
        "--spec-draft-n-max",
        "8",
        "--host",
        "0.0.0.0",
        "--port",
        str(CONFIG["PORT"]),
        "-ngl",
        "99",
        "-ts",
        "1,1",
        "-sm",
        "layer",
        "-c",
        str(CONFIG["CONTEXT_SIZE"]),
        "--cache-type-k",
        "q4_0",
        "--cache-type-v",
        "q4_0",
        "-b",
        "1024",
        "-ub",
        "512",
        "-t",
        "4",
        "--parallel",
        "1",
        "--flash-attn",
        "on",
        "--cache-reuse",
        "1024",
        "--api-key",
        CONFIG["API_KEY"],
    ]
    log("SERVER", f"cmd: {' '.join(server_cmd)}", flush=True)
    with open(CONFIG["LOG_SERVER"], "w") as s_log:
        proc = subprocess.Popen(
            server_cmd, env=env, stdout=s_log, stderr=subprocess.STDOUT
        )
    health_url = f"http://127.0.0.1:{CONFIG['PORT']}/health"
    ready = False
    for _ in range(90):
        if proc.poll() is not None:
            with open(CONFIG["LOG_SERVER"]) as f:
                print(f.read(), flush=True)
            raise RuntimeError("llama-server crashed during startup")
        try:
            with urllib.request.urlopen(
                urllib.request.Request(health_url), timeout=2
            ) as resp:
                if resp.status == 200:
                    ready = True
                    break
        except Exception:
            time.sleep(2)
    if not ready:
        raise TimeoutError("llama-server health check timed out (180s)")
    log("SERVER", f"online on port {CONFIG['PORT']}", flush=True)
    return proc


def chat_once(content, max_tokens):
    payload = json.dumps(
        {
            "model": "qwen3.8-27b",
            "messages": [{"role": "user", "content": content}],
            "max_tokens": max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
            "temperature": 0.0,
        }
    ).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{CONFIG['PORT']}/v1/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {CONFIG['API_KEY']}",
        },
    )
    t0 = time.time()
    first_token = None
    tokens = 0
    resp = urllib.request.urlopen(req, timeout=900)
    try:
        buf = b""
        while True:
            chunk = resp.read(4096)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line.startswith(b"data:"):
                    continue
                data = line[5:].strip()
                if data == b"[DONE]":
                    continue
                try:
                    obj = json.loads(data)
                except Exception:  # noqa: S112 — SSE keepalives/comments are not JSON
                    continue
                usage = obj.get("usage")
                if usage and usage.get("completion_tokens"):
                    tokens = usage["completion_tokens"]
                choices = obj.get("choices") or []
                if not choices:
                    continue
                content_piece = (choices[0].get("delta") or {}).get("content")
                if content_piece and first_token is None:
                    first_token = time.time()
    finally:
        resp.close()
    end = time.time()
    ttft = (first_token - t0) if first_token else None
    gen_time = (end - first_token) if first_token else (end - t0)
    return {
        "ttft_s": round(ttft, 2) if ttft else None,
        "tokens": tokens,
        "gen_s": round(gen_time, 2),
        "tps": round(tokens / gen_time, 2) if tokens and gen_time else 0.0,
    }


def main():
    log("BENCH", f"variant={VARIANT} shard={SHARD_ID + 1}/{TOTAL_SHARDS}", flush=True)
    n_gpu = gpu_guard()
    server_bin = ensure_assets()
    bv = run_cmd([server_bin, "--version"], check=False)
    bin_version = ((bv.stdout or "") + (bv.stderr or "")).strip().splitlines()
    bin_version = bin_version[0][:120] if bin_version else "unknown"
    proc = start_server(server_bin)
    try:
        results = []
        for p in PROMPTS:
            log(
                "BENCH",
                f"prompt={p['name']} max_tokens={p['max_tokens']}...",
                flush=True,
            )
            r = chat_once(p["content"], p["max_tokens"])
            r["prompt"] = p["name"]
            r["max_tokens"] = p["max_tokens"]
            results.append(r)
            log(
                "BENCH",
                f"{p['name']}: ttft={r['ttft_s']}s tokens={r['tokens']} t/s={r['tps']}",
                flush=True,
            )
        summary = {
            "variant": VARIANT,
            "binary": "v0.4.1",
            "binary_version": bin_version,
            "n_gpu": n_gpu,
            "context": CONFIG["CONTEXT_SIZE"],
            "batch": "1024/512",
            "speculative": True,
            "draft_quant": "Q8_0",
            "results": results,
        }
        print("=" * 60, flush=True)
        print(
            "VARIANT: draft | binary v0.4.1 | speculative draft-dflash ON", flush=True
        )
        for r in results:
            print(
                f"  {r['prompt']}: ttft={r['ttft_s']}s tokens={r['tokens']} time={r['gen_s']}s t/s={r['tps']}",
                flush=True,
            )
        print("=" * 60, flush=True)
        print("===BENCHMARK_JSON_START===", flush=True)
        print(json.dumps(summary, indent=2), flush=True)
        print("===BENCHMARK_JSON_END===", flush=True)
        out_path = os.path.join(
            WORKING_DIR, f"benchmark_results_{VARIANT}_shard{SHARD_ID}.json"
        )
        with open(out_path, "w") as f:
            json.dump(summary, f, indent=2)
        log("BENCH", f"wrote {out_path}", flush=True)
    finally:
        try:
            proc.terminate()
        except Exception:
            pass


if __name__ == "__main__":
    main()
