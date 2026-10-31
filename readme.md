# Kaggle batch runner and kernel monitor
<img width="1875" height="998" alt="image" src="https://github.com/user-attachments/assets/e1244a55-d421-4bd4-83a8-a053556fe120" />

A FastAPI runner and local web UI for long-running Kaggle jobs. It divides parameter sweeps into shards, tracks runtime against the 12-hour session boundary, and pulls logs without keeping an open browser tab.

---

## What it handles

- Kernel dispatch: Launches remote notebooks with isolated runtime configurations and dedicated working directories.
- Parameter sharding: Splits dataset slices or evaluation sweeps across independent kernel runs by injecting shard indices (`SHARD_ID`, `START_INDEX`, `END_INDEX`) into the notebook environment.
- Session boundary alerts: Tracks the 12-hour kernel wall-clock limit, pinging Telegram at the 11-hour mark and at session termination.
- Pre-flight validation: Runs a 5-minute test timeout before launching long inference jobs.
- Hardware selection: Configures target hardware (CPU, single T4, dual T4, or TPU v3-8) at launch time.
- Real-time logging: Tails kernel output to a local browser view through WebSockets and saves raw log files locally.
- Artifact downloads: Streams generated output files and zipped archives directly to disk without loading entire archives into memory.
- Execution history: Lists past runs with links back to the Kaggle notebook.
- Port-to-Kaggle skill: Prepares `.py` and `.ipynb` files for kernel execution by handling line-buffered logs and scratch paths.
- Access control: Optional shared-secret login (`APP_AUTH_TOKEN`) with HMAC-signed HttpOnly session cookies.
- Interface: Clean dark UI using bundled pixel-art glyphs and local fonts without external CDN dependencies.

---

## Quick start

### Using Docker (recommended)

1. Configure `.env` (edit `.env.example`):
```env
# Kaggle API access token
KAGGLE_APIKEYS=your_kaggle_access_token

# Dashboard login secret. Leave empty to disable auth for local development.
APP_AUTH_TOKEN=a-long-random-string

# Telegram alerts: the bot sends direct messages when runs start, warn, or finish.
# Get your numeric ID from @userinfobot, then start a chat with your bot once.
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=123456789

# Hugging Face read token for model and dataset downloads.
HF_TOKEN=hf_read_token
```

2. Build the container:
```bash
docker compose build
```

3. Start the container:
```bash
docker compose up
```

---

### Local installation with uv

1. Install dependencies:
```bash
uv sync --locked
```

2. Configure `.env`:
```env
KAGGLE_APIKEYS=your_kaggle_access_token
APP_AUTH_TOKEN=a-long-random-string
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=123456789
HF_TOKEN=hf_read_token
```
Credentials and Telegram settings can also be updated directly from the web interface.

3. Start the server:
```bash
uv run fastapi run app/main.py 
```
The server binds to `127.0.0.1:8000` by default. Override with `APP_HOST=0.0.0.0` and `APP_PORT=8000` in `.env` only if you need network access; set `APP_AUTH_TOKEN` first.

4. Open the dashboard:
Navigate to [http://localhost:8000](http://localhost:8000) in your browser and sign in with your `APP_AUTH_TOKEN`.

5. Run tests and linting:
```bash
# Run tests
uv run pytest tests

# Run linter
uv run ruff check .
```

---

## How dispatch works

- Authentication: Access tokens are exported per subprocess via the `KAGGLE_API_TOKEN` environment variable because modern Kaggle CLI versions do not use `$KAGGLE_CONFIG_DIR/access_token`. The system never alters global files in `~/.kaggle`.
- Kernel identity: Kaggle tracks notebooks by their slugified title. Pushing an existing title updates that kernel version; launching with a new title creates a distinct kernel. The launcher prevents duplicate launches while a kernel with the same title is actively running.
- Notebook normalization: Missing `kernelspec` metadata is set to Python 3 automatically, and raw Python code submitted as `.ipynb` is wrapped into notebook cells before upload.
- Concurrency limits: The runner validates active GPU sessions against Kaggle limits before dispatching new workloads. A workload panel displays job progress with an option to stop active runs.
- Environment injection: Variables specified in `.env` (such as `HF_TOKEN`) are injected into the notebook preamble at launch time.

---

## Porting a script for Kaggle

Install the `port-to-kaggle` helper skill:
```bash
python skills/port-to-kaggle/scripts/port_to_kaggle.py in.py --out out.py --title "My Job" --accelerator nvidia-tesla-t4-x2
# report only: add --check
```

The script handles notebook normalization (kernelspec, raw Python wrap), fallback shard defaults (`SHARD_ID`, `START_INDEX`, which the runner overrides at push), line-buffered logging, and path resolution for working and scratch directories. It flags missing GPU checks, long titles, hardcoded credentials, and unsharded output paths.

---

## Security notes

- The server binds to loopback unless explicitly overridden with `APP_HOST`.
- When `APP_AUTH_TOKEN` is set, all routes (including the WebSocket log stream) require a signed session cookie. Sessions last 7 days and invalidate immediately if the secret is changed.
- API keys are stored locally in `data/kaggle_automation.db` and masked in the UI.
- Test suites run in an isolated directory (`AUTOMATION_DATA_DIR`) without touching stored data.

---

## Sharding large batch jobs

When dividing a task of 10,000,000 items across 4 parallel shards, each kernel run receives injected boundary variables at the top of its notebook:

```python
# Injected by the runner before dispatch
SHARD_ID = 0
TOTAL_SHARDS = 4
START_INDEX = 0
END_INDEX = 2500000
TOTAL_ITEMS = 10000000

for item_id in range(START_INDEX, END_INDEX):
    process(item_id)
```

---

## LLM inference benchmarks

Two scripts in `benchmarks/` serve Qwen3.8-27B (Q4_K_XL, 128K context, q4_0 KV cache) through llama-server on 2x T4 GPUs and measure response times across fixed test prompts.
Token counts are reported directly from the server via `stream_options.include_usage`.
Each run writes a JSON summary and leaves outputs in `/kaggle/working`.

The standard configuration is available as the default preset in the Single Run tab (`GET /api/presets`).

> Public web tunneling scripts (`serve_qwen3_8_*.py`) were removed. Exposing a persistent model server to the public internet via third-party tunnels conflicts with Kaggle Acceptable Use policies regarding open server hosting. Use dedicated cloud instances or Kaggle Models for persistent public endpoints.

Round one used a warmup prompt (8 tokens), an essay generation prompt (256 tokens), and a code task (256 tokens):

| prompt | plain, v0.4.0, batch 512/256 | draft-dflash, v0.4.1, Q8_0 draft, batch 1024/512 |
|---|---|---|
| warmup, 8 tokens | 98.82s total (cold load) | 106.11s total (cold load) |
| essay, 256 tokens | 11.56s to first token, 12.7s total, 20.16 t/s | 8.11s to first token, 19.26s total, 13.29 t/s |
| code, 256 tokens | 21.99s total, 11.64 t/s | 18.31s total, 13.99 t/s |

Round two evaluated code generation (synthesis, bug fixing, and completion at 256 tokens each) and recorded draft model acceptance rates:

| prompt | plain, v0.4.0 | draft-dflash, v0.4.1, acceptance |
|---|---|---|
| warmup, 8 tokens | 99.52s total (cold load) | 106.11s total (cold load), 0.7143 |
| code-synth, 256 tokens | 1.7s to first token (est), 23.72s total, 10.79 t/s | 29.82s total, 8.58 t/s, 0.4424 |
| code-fix, 256 tokens | 2.36s to first token (est), 21.78s total, 11.76 t/s | 22.0s total, 11.64 t/s, 0.4641 |
| code-complete, 256 tokens | 21.95s to first token, 21.95s total, 11.66 t/s | 17.41s total, 14.7 t/s, 0.6059 |

Draft acceptance averaged 0.5567 across the round. First-token times were estimated because the model streams reasoning tokens before final output. The draft build generates initial essay tokens faster (8.11s compared to 11.56s) but requires a 28GB secondary download without yielding sustained throughput advantages, so the standard configuration remains default.
