# Kaggle Multi-Account Automation Platform

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
- **Authentication**: Optional shared-secret login (`APP_AUTH_TOKEN`) with HMAC-signed HttpOnly cookies — protects every route including WebSockets.
- **Branding**: Geist Pixel display font as the site-wide default, Geist Mono terminal font and favicon pack served fully locally (no CDN font/icon dependencies). Icons are bundled pixel-art glyphs (Pixelarticons v2.4.1, MIT) rendered in mono via `currentColor` — raw SVGs live in `app/static/icons/pixel/`.
- **Modern Flat Dark UI**: Responsive dashboard with flat slate styling — no gradients, no glow effects.

---

## Quick Start

### 1. Activate Environment & Install Dependencies
```bash
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2. Configure `.env`
```env
# Kaggle access tokens (comma-separated; auto-registered on startup)
KAGGLE_APIKEYS=your_kaggle_access_token_1,your_kaggle_access_token_2

# Dashboard login secret. Leave EMPTY to disable auth (local dev only).
APP_AUTH_TOKEN=a-long-random-string

# Telegram alerts: the bot DMs this account when runs start/warn/finish.
# Get your numeric ID from @userinfobot, then press START on your bot once.
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=123456789
```
*(Accounts and Telegram credentials can also be managed directly from the UI — UI values override `.env`.)*

### 3. Start the Server
```bash
python run.py
```
The server binds to `127.0.0.1:8000` by default (safe). Override with `APP_HOST=0.0.0.0` / `APP_PORT=8000` in `.env` **only if you understand the exposure** — set `APP_AUTH_TOKEN` first.

### 4. Open the Dashboard
Navigate to [http://localhost:8000](http://localhost:8000) in your browser and sign in with your `APP_AUTH_TOKEN`.

---

## How Dispatch Works (Kaggle CLI 2.x)

- **Auth**: each account's access token is exported to its own subprocess via the `KAGGLE_API_TOKEN` env var — the modern kaggle CLI does not read `$KAGGLE_CONFIG_DIR/access_token`. `~/.kaggle` is never touched, so accounts stay fully isolated.
- **Kernel identity**: Kaggle keys notebooks by the slugified *title*. Relaunching a run with the same title on the same account creates a new **version** of that kernel; use a different title for a fresh kernel.
- **Notebooks are normalized before push**: missing `kernelspec` is injected (python3) and raw Python pasted as `.ipynb` is wrapped into a valid notebook cell automatically.

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