# Kaggle Multi-Account Automation Platform

A centralized dashboard and FastAPI backend to orchestrate, monitor, and distribute workloads across multiple Kaggle accounts using the Kaggle CLI.

---

## Features

- **Multi-Account Dashboard**: Manage multiple Kaggle accounts in parallel with zero credential collisions (isolated `KAGGLE_CONFIG_DIR`).
- **Quota Meters**: Live tracking of weekly GPU (`T4 x 2`, `P100`) and TPU (`v3-8`) quota usage with visual gauges.
- **Hardware Accelerators**: Select `Default / CPU`, `T4 GPU x 2`, `T4 GPU x 1`, or `TPU VM v3-8`.
- **Pre-Flight Trial Run**: Run quick validation passes (e.g. 5-minute timeout) before committing 12-hour full runs.
- **12-Hour Session Tracker & Telegram Alerts**:
  - Auto-notifies Telegram channels when runs start.
  - Sends a 1-hour warning alert at the 11-hour mark.
  - Sends a cutoff alert at the 12-hour Kaggle runtime limit.
  - Sends immediate completion and error alerts.
- **Distributed Workload Sharder**: Partition large tasks (e.g. 10,000,000 iterations or parameter batches) evenly across all registered Kaggle accounts, with auto-injected shard parameters (`SHARD_ID`, `TOTAL_SHARDS`, `START_INDEX`, `END_INDEX`) and parallel execution.
- **Live Output Streaming**: Real-time streaming console over WebSockets with auto-scroll and full log downloads.
- **Output Artifacts Explorer**: Browse generated files and download single files or full `.zip` archives with 1 click.
- **Run Catalog**: Full execution history with direct clickable Kaggle notebook URLs.
- **Modern Dark UI**: Responsive dashboard with dark cyberpunk/slate styling.

---

## Quick Start

### 1. Activate Environment & Install Dependencies
```bash
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2. Configure `.env` (Optional)
```env
KAGGLE_APIKEYS=your_kaggle_access_token_1,your_kaggle_access_token_2
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=-1001234567890
```
*(Accounts and Telegram keys can also be added dynamically directly from the UI!)*

### 3. Start the Server
```bash
python run.py
```
*(Or with uvicorn directly: `uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload --reload-dir app`)*

### 4. Open the Dashboard
Navigate to [http://localhost:8000](http://localhost:8000) in your browser.

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