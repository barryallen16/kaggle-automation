# Kaggle batch runner and kernel monitor
<img width="1875" height="998" alt="image" src="https://github.com/user-attachments/assets/e1244a55-d421-4bd4-83a8-a053556fe120" />

A FastAPI runner and web UI for long-running Kaggle notebooks.
It divides parameter sweeps into shards, checks the 12-hour session limit, sends Telegram alerts, and pulls logs and outputs.

## What it does

The runner launches notebooks across one or more Kaggle accounts, passes environment variables such as `HF_TOKEN` into the kernel preamble, and injects `SHARD_ID`, `START_INDEX`, `END_INDEX`, and `TOTAL_ITEMS` for sharded sweeps.
Before dispatching runs, it checks active GPU sessions against Kaggle's two-session cap per account and tracks the 12-hour wall-clock boundary.
You can launch a 5-minute trial run, choose `None` (CPU), `GPU T4 x2`, or `TPU v5e-8` on free-tier accounts (or passthrough shapes like `L4`, `A100`, `H100`, `T4 High Memory`, `RTX Pro 6000`, and `TPU v6e-8`), stream logs over WebSockets, and download individual output files or zip archives.
Optional shared-secret auth (`APP_AUTH_TOKEN`) uses HMAC-signed HttpOnly cookies or `Authorization: Bearer` headers.
Two local agent skills live in `skills/`: `port-to-kaggle` prepares `.py` and `.ipynb` files, and `kaggle-runner` dispatches runs, streams logs, downloads outputs, and stops jobs against `kabila-aws.isroot.in` or localhost.
The UI uses local fonts and pixel icons bundled under `app/static/` with Tailwind loaded from `cdn.tailwindcss.com`.

## Quick start

### Using Docker

1. Copy `.env.example` to `.env` and set `KAGGLE_APIKEYS`, `APP_AUTH_TOKEN`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, and `HF_TOKEN`.
2. Build and start the container (pass `--env-file .env` or add `env_file: .env` in `docker-compose.yml` so your secrets reach the container):
```bash
docker compose build
docker compose --env-file .env up
```

### Local setup with uv

1. Install dependencies:
```bash
uv sync --locked
```

2. Create `.env` from `.env.example`:
```env
KAGGLE_APIKEYS=your_kaggle_access_token
APP_AUTH_TOKEN=a-long-random-string
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=123456789
HF_TOKEN=hf_read_token
```
Credentials and Telegram settings can also be changed in the web UI.

3. Start the server:
```bash
uv run fastapi run app/main.py
```
The server binds to `127.0.0.1:8000` by default.
Override with `APP_HOST=0.0.0.0` and `APP_PORT=8000` only after setting `APP_AUTH_TOKEN`.

4. Open [http://localhost:8000](http://localhost:8000) in a browser and sign in with `APP_AUTH_TOKEN`.

5. Run checks:
```bash
uv run pytest tests
uv run ruff check .
```

## How dispatch works

Subprocesses export `KAGGLE_API_TOKEN` in the environment instead of writing `~/.kaggle` files because Kaggle CLI `2.2+` reads that token variable directly.
Version-check banners printed by `kaggle` CLI are stripped before parsing CSV or status output.
Kaggle tracks notebooks by slugified title, so pushing an existing title adds a new version while a new title creates a separate kernel.
The launcher blocks duplicate pushes while a kernel with the same title is running, sets missing Python 3 `kernelspec` metadata, and wraps raw `.py` files into notebook cells before upload.

## Porting a script for Kaggle

Prepare a script or notebook with `port-to-kaggle`:
```bash
python skills/port-to-kaggle/scripts/port_to_kaggle.py in.py --out out.py --title "My Job" --accelerator nvidia-tesla-t4-x2
```
Add `--check` to report issues without writing `out.py`.
The script normalizes notebook metadata, inserts fallback shard variables, sets line-buffered output, sets `WORKING_DIR` and `SCRATCH_DIR` (`/kaggle/tmp`), and flags missing GPU checks, titles over 50 characters, hardcoded credentials, and unsharded output filenames.

Manage runs from the CLI with `kaggle-runner`:
```bash
python skills/kaggle-runner/scripts/kaggle_runner.py auth --token "<APP_AUTH_TOKEN>"
python skills/kaggle-runner/scripts/kaggle_runner.py
python skills/kaggle-runner/scripts/kaggle_runner.py run out.py --title "My Job" --accelerator nvidia-tesla-t4-x2
```

## Security notes

When `APP_AUTH_TOKEN` is set, HTTP routes accept either the signed cookie or an `Authorization: Bearer` header, and `/ws/runs/{run_id}/logs` accepts the signed cookie.
Cookies stay valid for 7 days and invalidate when `APP_AUTH_TOKEN` changes.
API keys live in `data/kaggle_automation.db` and are masked in account listings.
Test suites set `AUTOMATION_DATA_DIR` to a temporary directory.

## Sharding large batch jobs

When splitting 10,000,000 items across 4 shards, each kernel gets boundary variables at the top of the notebook:

```python
SHARD_ID = 0
TOTAL_SHARDS = 4
START_INDEX = 0
END_INDEX = 2500000
TOTAL_ITEMS = 10000000

for item_id in range(START_INDEX, END_INDEX):
    process(item_id)
```
