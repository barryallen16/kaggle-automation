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
- Kaggle runner skill: Dispatches, monitors, and stops runs on kabila-aws.isroot.in with persistent session credentials.
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
