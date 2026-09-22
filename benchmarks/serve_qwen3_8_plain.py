# Working Qwen3.8-27B serve: plain (no speculative decoding) on 2xT4 + zrok share.
# Same flow as serve_qwen3_8_draft.py, minus the draft model. Binary v0.4.0
# served 256-token prompts at 20.2 t/s (essay) in benchmarks. ZROK_TOKEN comes
# from the environment (dashboard injects it at push) and is never baked in.
# ponytail: frozen single-file push snapshot — serve_qwen3_8_draft.py with
# SPEC_USE_DRAFT=0 is canonical for local runs; keep this file standalone.

import glob
import json
import os
import re
import shutil
import subprocess
import time
import urllib.request

# ==============================================================================
# PIPELINE CONFIGURATION (env-overridable; ZROK_TOKEN has no default)
# ==============================================================================
CONFIG = {
    "MODEL_URL": os.environ.get("MODEL_URL")
    or "https://huggingface.co/unsloth/Qwen3.8-27B-GGUF/resolve/main/Qwen3.8-27B-UD-Q4_K_XL.gguf",
    "MODEL_PATH": os.environ.get("MODEL_PATH") or "/kaggle/tmp/qwen3.8-27b-q4.gguf",
    "BINARY_URL": os.environ.get("BINARY_URL")
    or "https://github.com/ai-dock/llama.cpp-cuda/releases/download/v0.4.0/llama.cpp-v0.4.0-cuda-12.8-amd64.tar.gz",
    "BIN_DIR": os.environ.get("BIN_DIR") or "/kaggle/tmp/llama_bin",
    "PORT": int(os.environ.get("PORT") or 8080),
    "API_KEY": os.environ.get("LLM_API_KEY") or "kaggle-opencode-key",
    "CONTEXT_SIZE": int(os.environ.get("CONTEXT_SIZE") or 131072),
    "ZROK_TOKEN": os.environ.get("ZROK_TOKEN") or "",
    "ZROK_NAME": os.environ.get("ZROK_NAME") or "llama",
    "LOG_SERVER": os.environ.get("LOG_SERVER") or "/kaggle/tmp/llama_server.log",
    "LOG_ZROK": os.environ.get("LOG_ZROK") or "/kaggle/tmp/zrok_share.log",
    "SHARE_TARGET": os.environ.get("SHARE_TARGET") or "http://127.0.0.1:8080",
}

if not CONFIG["ZROK_TOKEN"]:
    raise RuntimeError(
        "ZROK_TOKEN is not set — export it (dashboard injects it at push) before launching"
    )


def log(stage: str, msg: str):
    print(f"[{stage}] {msg}")


def run_cmd(cmd, check=True, capture=True, env=None):
    r = subprocess.run(cmd, text=True, capture_output=capture, env=env, check=False)
    if check and r.returncode != 0:
        err = (r.stdout or "") + (r.stderr or "")
        raise RuntimeError(f"Command failed ({r.returncode}): {' '.join(cmd)}\n{err}")
    return r, (r.stdout or "") + (r.stderr or "")


# ==============================================================================
# 1. ENVIRONMENT & DEPENDENCIES SETUP
# ==============================================================================
os.makedirs("/kaggle/tmp", exist_ok=True)
os.makedirs(CONFIG["BIN_DIR"], exist_ok=True)

log("SETUP", "Installing aria2 and zrok2...")
subprocess.run(
    "apt-get update -qq && apt-get install -y -qq aria2", shell=True, check=True
)
if not shutil.which("zrok2"):
    subprocess.run(
        "curl -sSf https://get.openziti.io/install.bash | sudo bash -s zrok2",
        shell=True,
        check=True,
    )

# ==============================================================================
# 2. ASSET DOWNLOADS (MODEL & LLAMA-SERVER BINARY)
# ==============================================================================
if not os.path.exists(CONFIG["MODEL_PATH"]):
    log("DOWNLOAD", f"Downloading model to {CONFIG['MODEL_PATH']}...")
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
            "/kaggle/tmp",
            "-o",
            os.path.basename(CONFIG["MODEL_PATH"]),
            CONFIG["MODEL_URL"],
        ]
    )
else:
    log("DOWNLOAD", "Model binary already present. Skipping download.")

server_bin = os.path.join(CONFIG["BIN_DIR"], "llama-server")
if not os.path.exists(server_bin):
    log("DOWNLOAD", "Fetching CUDA prebuilt binary...")
    tar_path = "/kaggle/tmp/llama.tar.gz"
    run_cmd(["wget", "-q", CONFIG["BINARY_URL"], "-O", tar_path])
    run_cmd(
        ["tar", "-xzf", tar_path, "-C", CONFIG["BIN_DIR"], "--strip-components=1"],
        check=False,
    )

    candidates = glob.glob(f"{CONFIG['BIN_DIR']}/**/llama-server", recursive=True)
    if not candidates:
        raise FileNotFoundError("Could not locate llama-server executable in archive.")
    server_bin = candidates[0]
    os.chmod(server_bin, 0o755)

log("SETUP", f"Using server binary at: {server_bin}")

# ==============================================================================
# 3. LAUNCH LLAMA-SERVER
# ==============================================================================
log("SERVER", "Terminating lingering llama-server instances...")
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
    "512",
    "-ub",
    "256",
    "--flash-attn",
    "on",
    "--cache-reuse",
    "1024",
    "--api-key",
    CONFIG["API_KEY"],
]
log("SERVER", "Starting llama-server background process...")
with open(CONFIG["LOG_SERVER"], "w") as s_log:
    server_proc = subprocess.Popen(
        server_cmd, env=env, stdout=s_log, stderr=subprocess.STDOUT
    )

# Active HTTP health check polling
log("SERVER", "Awaiting server health confirmation...")
health_url = f"http://127.0.0.1:{CONFIG['PORT']}/health"
server_ready = False

for _ in range(90):
    if server_proc.poll() is not None:
        with open(CONFIG["LOG_SERVER"], "r") as f:
            print(f.read())
        raise RuntimeError("llama-server crashed during startup.")
    try:
        req = urllib.request.Request(health_url)
        with urllib.request.urlopen(req, timeout=2) as resp:
            if resp.status == 200:
                server_ready = True
                break
    except Exception:
        time.sleep(2)

if not server_ready:
    raise TimeoutError("llama-server failed to respond to health checks within 180s.")

log("SERVER", f"llama-server online on port {CONFIG['PORT']}.")
# ==============================================================================
# 3b. WARMUP — pay the first-token cost NOW, before the public URL exists
# ==============================================================================
log("SERVER", "Warming up inference path (absorbs first-token latency)...")
warmup_payload = json.dumps(
    {
        "model": "qwen3.8-27b",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 8,
        "stream": False,
        "temperature": 0.0,
    }
).encode()
req = urllib.request.Request(
    f"http://127.0.0.1:{CONFIG['PORT']}/v1/chat/completions",
    data=warmup_payload,
    headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {CONFIG['API_KEY']}",
    },
)
t0 = time.time()
with urllib.request.urlopen(req, timeout=900) as resp:
    resp.read()
log(
    "SERVER",
    f"Warmup completed in {time.time() - t0:.1f}s. Subsequent first tokens should be fast.",
)

zrok_bin = shutil.which("zrok2") or "/usr/bin/zrok2"

# 1. Reset local state and re-establish the environment cleanly.
run_cmd([zrok_bin, "disable"], check=False)
log("ZROK", "Enabling zrok environment...")
run_cmd([zrok_bin, "enable", CONFIG["ZROK_TOKEN"]])


# --- Get the real env id from local config, NOT from `status` (which redacts it as <<SET>>) ---
def read_ziti_identity():
    candidates = [
        os.path.expanduser("~/.zrok/environment.json"),
        os.path.expanduser("~/.zrok2/environment.json"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            try:
                with open(path) as f:
                    data = json.load(f)
                zid = data.get("ziti_identity") or data.get("zId")
                if zid:
                    log("ZROK", f"Read env id '{zid}' from {path}")
                    return zid
            except Exception as e:
                log("ZROK", f"Failed to parse {path}: {e}")
    return None


current_env_id = read_ziti_identity()
log("ZROK", f"Active Environment ID: {current_env_id}")

if current_env_id is None:
    log(
        "ZROK",
        "FATAL: no environment.json found under ~/.zrok or ~/.zrok2 after enable.",
    )
    raise RuntimeError(
        "could not locate local zrok environment config — refusing to run purge logic blind"
    )

# 2. Purge stale/orphaned environments (never the current one).
log("ZROK", "Scanning for stale environments...")
_, overview_json = run_cmd([zrok_bin, "overview", "--json"], check=False)
_, overview_text = run_cmd([zrok_bin, "overview"], check=False)

stale_envs = set()
try:
    data = json.loads(overview_json)
    for e in data.get("environments", []):
        env_obj = e.get("environment", e)  # nested in real zrok output
        eid = env_obj.get("zId") or env_obj.get("id")
        if eid and eid != current_env_id:
            stale_envs.add(eid)
except Exception:
    pass

for blob in (overview_json, overview_text):
    for eid in re.findall(r"\bzId[\"'\s:]+([A-Za-z0-9]{8,})", blob):
        if eid != current_env_id:
            stale_envs.add(eid)

if stale_envs:
    log("ZROK", f"Purging stale environments: {stale_envs}")
    for eid in stale_envs:
        _, del_out = run_cmd(
            [zrok_bin, "delete", "environment", eid, "--force"], check=False
        )
        log("ZROK", f"  {eid}: {del_out.strip() or 'deleted'}")
else:
    log("ZROK", "No stale environments found.")

# Kill lingering share/access processes BEFORE touching the reserved name —
# a live share attached to the name blocks deletion.
subprocess.run(["pkill", "-9", "-f", "zrok"], stderr=subprocess.DEVNULL, check=False)
time.sleep(2)

# 3. Resolve namespace.
log("ZROK", "Looking up available namespace...")
_, ns_out = run_cmd([zrok_bin, "list", "namespaces", "--json"], check=False)
namespace_token = "public"
try:
    ns_data = json.loads(ns_out)
    if isinstance(ns_data, list) and ns_data:
        namespace_token = ns_data[0].get("namespaceToken", namespace_token)
except Exception:
    log("ZROK", "Could not parse namespace list JSON, defaulting to 'public'.")
log("ZROK", f"Using namespace: {namespace_token}")

# 4. Release + reserve the name — single attempt, logged, with one retry
#    if the backend hasn't yet noticed the killed share process.
log("ZROK", f"Releasing any stale reservation of '{CONFIG['ZROK_NAME']}'...")
_, release_out = run_cmd(
    [zrok_bin, "delete", "name", "-n", namespace_token, CONFIG["ZROK_NAME"]],
    check=False,
)
log("ZROK", release_out.strip() or "no existing reservation found")

if "still attached to share" in release_out:
    log("ZROK", "Name still attached; waiting and retrying release once...")
    time.sleep(3)
    _, release_out = run_cmd(
        [zrok_bin, "delete", "name", "-n", namespace_token, CONFIG["ZROK_NAME"]],
        check=False,
    )
    log("ZROK", release_out.strip() or "released on retry")

log("ZROK", f"Reserving name '{CONFIG['ZROK_NAME']}'...")
_, create_out = run_cmd(
    [zrok_bin, "create", "name", "-n", namespace_token, CONFIG["ZROK_NAME"]],
    check=False,
)
log("ZROK", create_out.strip())

name_selection = f"{namespace_token}:{CONFIG['ZROK_NAME']}"
log("ZROK", f"Bound to reserved name selection: {name_selection}")
# 5. Attach a live public share to the reserved name.
#    --headless disables zrok's full-screen terminal UI (which otherwise
#    tries to open /dev/tty directly and crashes under a piped subprocess
#    with no real terminal attached) and logs to stdout instead.
share_target = CONFIG.get("SHARE_TARGET")
if not share_target:
    raise RuntimeError("CONFIG['SHARE_TARGET'] is not set — e.g. '127.0.0.1:8080'")

log("ZROK", f"Starting public share on '{name_selection}' -> {share_target}...")

share_log_path = "/tmp/zrok_share.log"
share_log_file = open(share_log_path, "w")  # noqa: SIM115 — handle stays open as Popen stdout

share_proc = subprocess.Popen(
    [zrok_bin, "share", "public", share_target, "-n", name_selection, "--headless"],
    stdout=share_log_file,
    stderr=subprocess.STDOUT,
)
log(
    "ZROK", f"Share process started (pid={share_proc.pid}), logging to {share_log_path}"
)

public_url = None
deadline = time.time() + 60
# zrok headless logs a bare hostname inside a JSON string, e.g.
#   "msg":"access your zrok share at the following endpoints:\n llama.shares.zrok.io"
# so match the name followed by at least one .segment, and stop at the JSON quote.
url_re = re.compile(
    r"(?:https?://)?(" + re.escape(CONFIG["ZROK_NAME"]) + r"(?:\.[A-Za-z0-9-]+)+)"
)
while time.time() < deadline:
    if share_proc.poll() is not None:
        log(
            "ZROK",
            f"FATAL: share process exited early (code={share_proc.returncode}); see {share_log_path}",
        )
        break
    with open(share_log_path) as f:
        content = f.read()
    m = url_re.search(content)
    if m:
        public_url = m.group(1)
        if not public_url.startswith("http"):
            public_url = "https://" + public_url
        break
    time.sleep(1)

log("ZROK", f"public_url={public_url or 'NOT FOUND — see /tmp/zrok_share.log'}")
