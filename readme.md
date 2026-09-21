# Kaggle Multi-Account Automation Platform
<img width="1875" height="998" alt="image" src="https://github.com/user-attachments/assets/e1244a55-d421-4bd4-83a8-a053556fe120" />

A centralized dashboard and FastAPI backend to orchestrate, monitor, and distribute workloads across multiple Kaggle accounts using the Kaggle CLI.

---

## Features

- **Multi-Account Dashboard**: Manage multiple Kaggle accounts in parallel with zero credential collisions (isolated `KAGGLE_CONFIG_DIR`).
- **Quota Meters**: Live tracking of weekly GPU (`T4 x 2`, `P100`) and TPU (`v3-8`) quota usage with visual gauges.
- **Hardware Accelerators**: Select `Default / CPU`, `T4 GPU x 2`, `T4 GPU x 1`, or `TPU VM v3-8`.
- **Pre-Flight Trial Run**: Run quick validation passes (e.g. 5-minute timeout) before committing 12-hour full runs.
- **12-Hour Session Tracker & Telegram Alerts**:
  - Auto-notifies your Telegram account (bot direct messages via User ID) when runs start.
  - Sends a 1-hour warning alert at the 11-hour mark.
  - Sends a cutoff alert at the 12-hour Kaggle runtime limit.
  - Sends immediate completion and error alerts.
- **Distributed Workload Sharder**: Partition large tasks (e.g. 10,000,000 iterations or parameter batches) evenly across all registered Kaggle accounts, with auto-injected shard parameters (`SHARD_ID`, `TOTAL_SHARDS`, `START_INDEX`, `END_INDEX`) and parallel execution.
- **Live Output Streaming**: Real-time streaming console over WebSockets with auto-scroll and full log downloads.
- **Output Artifacts Explorer**: Browse generated files and download single files or full `.zip` archives (streamed from disk, never buffered in RAM) with 1 click.
- **Run Catalog**: Full execution history with direct clickable Kaggle notebook URLs.
- **Port-to-Kaggle Skill**: adapt any `.py`/`.ipynb` for single + distributed runs (`skills/port-to-kaggle`) — shard fallback, log buffering, working/scratch dirs, with warnings for GPU guard, title length and unsharded outputs.
- **Dashboard KPIs**: `Total Runs` = row count of the run catalog; `GPU-hours` = finished GPU runs only (`complete`/`error`/`stopped`/`canceled`), each capped at its `timeout_seconds` (12 h default, trial 300 s); `wks on 1 acct` = GPU-hours ÷ 30 h single-account weekly quota.
- **Authentication**: Optional shared-secret login (`APP_AUTH_TOKEN`) with HMAC-signed HttpOnly cookies — protects every route including WebSockets.
- **Branding**: Geist Pixel display font as the site-wide default, Geist Mono terminal font and favicon pack served fully locally (no CDN font/icon dependencies). Icons are bundled pixel-art glyphs (Pixelarticons v2.4.1, MIT) rendered in mono via `currentColor` — raw SVGs live in `app/static/icons/pixel/`.
- **Modern Flat Dark UI**: Responsive dashboard with flat slate styling — no gradients, no glow effects.

---

## Quick Start

### Using docker (recommended)

### 1. Configure `.env` (edit .env.example)
```env
# Kaggle access tokens (comma-separated; auto-registered on startup)
KAGGLE_APIKEYS=your_kaggle_access_token_1,your_kaggle_access_token_2

# Dashboard login secret. Leave EMPTY to disable auth (local dev only).
APP_AUTH_TOKEN=a-long-random-string

# Telegram alerts: the bot DMs this account when runs start/warn/finish.
# Get your numeric ID from @userinfobot, then press START on your bot once.
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=123456789

#hf read token for faster model, datasets downloads.
HF_TOKEN=hf_read_token

# zrok share token for the serve-script presets (injected into kernels at
# push, like HF_TOKEN). Get one at https://zrok.io. Empty = serve scripts
# refuse to start; benchmarks don't need it.
ZROK_TOKEN=your_zrok_token
```

### 2. Build the image
```bash
docker compose build
```

### 3. start container
```bash
docker compose up
```

---

### 1. Activate Environment & Install Dependencies using uv
```bash
uv sync --locked
```

### 2. Configure `.env` (edit .env.example)
```env
# Kaggle access tokens (comma-separated; auto-registered on startup)
KAGGLE_APIKEYS=your_kaggle_access_token_1,your_kaggle_access_token_2

# Dashboard login secret. Leave EMPTY to disable auth (local dev only).
APP_AUTH_TOKEN=a-long-random-string

# Telegram alerts: the bot DMs this account when runs start/warn/finish.
# Get your numeric ID from @userinfobot, then press START on your bot once.
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=123456789

#hf read token for faster model, datasets downloads.
HF_TOKEN=hf_read_token

# zrok share token for the serve-script presets (injected into kernels at
# push, like HF_TOKEN). Get one at https://zrok.io. Empty = serve scripts
# refuse to start; benchmarks don't need it.
ZROK_TOKEN=your_zrok_token
```
*(Accounts and Telegram credentials can also be managed directly from the UI — UI values override `.env`.)*

### 3. Start the Server
```bash
uv run fastapi run app/main.py 
```
The server binds to `127.0.0.1:8000` by default (safe). Override with `APP_HOST=0.0.0.0` / `APP_PORT=8000` in `.env` **only if you understand the exposure** — set `APP_AUTH_TOKEN` first.

### 4. Open the Dashboard
Navigate to [http://localhost:8000](http://localhost:8000) in your browser and sign in with your `APP_AUTH_TOKEN`.

### 5. Running Tests & Linting
```bash
# Run all tests
uv run pytest tests

# Run linter
uv run ruff check .
```

---

## How Dispatch Works (Kaggle CLI 2.x)

- **Auth**: each account's access token is exported to its own subprocess via the `KAGGLE_API_TOKEN` env var — the modern kaggle CLI does not read `$KAGGLE_CONFIG_DIR/access_token`. `~/.kaggle` is never touched, so accounts stay fully isolated.
- **Kernel identity**: Kaggle keys notebooks by the slugified *title*. Relaunching a run with the same title on the same account creates a new **version** of that kernel; use a different title for a fresh kernel. The launcher blocks duplicate same-title launches while one is still active.
- **Notebooks are normalized before push**: missing `kernelspec` is injected (python3) and raw Python pasted as `.ipynb` is wrapped into a valid notebook cell automatically.
- **Multi-session distribution**: the Distributed Runner defaults to **2 GPU sessions per account** (Kaggle's cap). It live-checks each account's active GPU sessions and silently reduces runners when slots are busy; the whole launch is validated atomically before anything is dispatched. A Recent Workloads panel shows progress with a Stop-All button per workload.
- **Secrets**: keys in `.env` (`HF_TOKEN`, …) are injected into every kernel as an environment preamble before user code — a READ-scoped HF token is enough for faster public-artifact downloads.

---

## Porting a Script for Kaggle

### Install via agent
Copy/paste into your CLI prompt:
```
Install the port-to-kaggle skill from https://github.com/barryallen16/kaggle-automation/tree/main/skills/port-to-kaggle, refer to skills/port-to-kaggle/SKILL.md for usage.
```
Or check `skills/port-to-kaggle/SKILL.md` for manual usage.

```bash
python skills/port-to-kaggle/scripts/port_to_kaggle.py in.py --out out.py --title "My Job" --accelerator nvidia-tesla-t4-x2
# report only: add --check
```

Auto-fixes: notebook normalization (kernelspec, raw-py wrap), standalone-safe shard fallback (`SHARD_ID`, `START_INDEX`, … — overridden by the distributor at push), line-buffered logs, `WORKING_DIR`/`SCRATCH_DIR` paths. Warns on missing GPU guard, `>50`-char titles, hardcoded secrets, datasets landing in `/kaggle/working`, unsharded `OUTPUT_FILE` and bare `pip install`. Secrets and `MAX_RUNTIME_MINUTES` are injected server-side at push. Reference port: `kaggle_batch_inference_task_a.py`. Installed globally as the `port-to-kaggle` skill (`~/.config/opencode/skills/`).

---

## Security Notes

- The server binds to loopback unless you explicitly override `APP_HOST`.
- With `APP_AUTH_TOKEN` set, every route — including the WebSocket log stream — requires a signed session cookie; sessions survive 7 days and are invalidated the moment you rotate the token.
- API keys are stored locally in `data/kaggle_automation.db` and never returned by the API (masked as `KGAT_a...xyz`).
- Tests run fully isolated (`AUTOMATION_DATA_DIR`) and never touch production data.

---

## Distributed Workload Example

When distributing a task of 10,000,000 items across 4 Kaggle accounts, each account automatically receives injected variables at the top of its notebook:

```python
# ==========================================
# AUTO-INJECTED WORKLOAD SHARD CONFIGURATION
# ==========================================
SHARD_ID = 0
TOTAL_SHARDS = 4
START_INDEX = 0
END_INDEX = 2500000
TOTAL_ITEMS = 10000000
# ==========================================

for item_id in range(START_INDEX, END_INDEX):
    # Your distributed processing logic here
    process(item_id)
```

---

## LLM serve benchmarks

Two scripts in `benchmarks/` serve Qwen3.8-27B (Q4_K_XL, 128K context, q4_0 KV cache) through llama-server on 2x T4 and time fixed prompts. Token counts come from the server (`stream_options.include_usage`). Each run prints a JSON block and leaves a per-shard copy under `/kaggle/working`. The matching `serve_qwen3_8_*.py` scripts run the same servers plus a zrok public share instead of timing; they need `ZROK_TOKEN` in the server `.env` (injected into the kernel at push, never stored in the file).

The plain build is the default preset in the Single Run tab (`GET /api/presets`).

Round one used a warmup `hi` (8 tokens), an essay (256 tokens), and a generic code task (256 tokens):

| prompt | plain, v0.4.0, batch 512/256 | draft-dflash, v0.4.1, Q8_0 draft, batch 1024/512 |
|---|---|---|
| warmup, 8 tokens | 98.82s total (cold load) | 106.11s total (cold load) |
| essay, 256 tokens | 11.56s to first token, 12.7s total, 20.16 t/s | 8.11s to first token, 19.26s total, 13.29 t/s |
| code, 256 tokens | 21.99s total, 11.64 t/s | 18.31s total, 13.99 t/s |

Round two ran code only (synthesis, bug fix, completion, 256 tokens each) and captured the server's acceptance rate on the draft build:

| prompt | plain, v0.4.0 | draft-dflash, v0.4.1, acceptance |
|---|---|---|
| warmup, 8 tokens | 99.52s total (cold load) | 106.11s total (cold load), 0.7143 |
| code-synth, 256 tokens | 1.7s to first token (est), 23.72s total, 10.79 t/s | 29.82s total, 8.58 t/s, 0.4424 |
| code-fix, 256 tokens | 2.36s to first token (est), 21.78s total, 11.76 t/s | 22.0s total, 11.64 t/s, 0.4641 |
| code-complete, 256 tokens | 21.95s to first token, 21.95s total, 11.66 t/s | 17.41s total, 14.7 t/s, 0.6059 |

Draft acceptance mean over the round is 0.5567, and throughput tracks it task by task. First-token times stayed estimated because this model streams reasoning traces before content. The plain code-complete delivery arrived in one trailing chunk, so its rate equals total time (256 / 21.95); the harness falls back to total time in that case instead of dividing by a sliver. The draft answers essay first tokens faster (8.11s vs 11.56s) but carries a second ~28GB download with no steady throughput gain, so plain stays the default.
