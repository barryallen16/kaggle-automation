# Kaggle Automation Platform — Changelog

> Complete record of all changes made during this session (August 22, 2026).
> From initial diagnosis through bug fixes, infrastructure improvements, and script testing.

---

## Table of Contents

1. [Initial Diagnosis](#1-initial-diagnosis)
2. [Clean Database Wipe](#2-clean-database-wipe)
3. [SQL Injection Fix](#3-sql-injection-fix)
4. [Fetch Quota Error Handling](#4-fetch-quota-error-handling)
5. [Temporary Directory Cleanup](#5-temporary-directory-cleanup)
6. [Windows Encoding Fix (PYTHONIOENCODING)](#6-windows-encoding-fix-pythonioencoding)
7. [Kaggle CLI Path Discovery](#7-kaggle-cli-path-discovery)
8. [Accelerator Mapping Fix (T4 x2 → P100 bug)](#8-accelerator-mapping-fix)
9. [Full Accelerator Map (12 GPU/TPU types)](#9-full-accelerator-map)
10. [Frontend Accelerator Dropdown Update](#10-frontend-accelerator-dropdown-update)
11. [409 Conflict Retry Logic](#11-409-conflict-retry-logic)
12. [Kaggle Package Upgrade](#12-kaggle-package-upgrade)
13. [Script Fixes (kaggle_batch_inference_task_a.py)](#13-script-fixes)
14. [End-to-End Verification](#14-end-to-end-verification)

---

## 1. Initial Diagnosis

**Problem:** The application was not working as expected. Investigation revealed multiple compounding issues.

**Findings:**
- All 20+ accounts in the database had fake usernames (`kaggle_user_05b254`, `kaggle_user_0790d8`, etc.) instead of real Kaggle usernames
- All quota data showed 0 GPU / 0 TPU hours — `kaggle quota -v` was failing silently
- The `update_run_telegram_flag()` function had a SQL injection vulnerability via f-string column name interpolation
- `fetch_quota()` didn't handle CLI errors gracefully — returned empty data with no error indication
- 65+ junk account directories existed in `data/accounts/` from repeated failed initialization attempts

**Root Cause:** The original code hardcoded `"kaggle"` as the CLI command. Since the kaggle binary was only in the virtualenv (not on system PATH), all CLI calls failed, username resolution fell through to the `kaggle_user_{uuid}` fallback, and accounts were permanently saved with fake names.

---

## 2. Clean Database Wipe

**Why:** The database and filesystem were polluted with 20+ fake accounts, test data from `test_automation.py`, and orphaned config directories. Starting clean was necessary.

**What changed:**
- Deleted `data/kaggle_automation.db` (all accounts, runs, workloads, settings)
- Wiped all directories from `data/accounts/`
- Removed stale test accounts (`test_user_unit`) and test runs from the database
- Cleaned up orphaned temp directories created during username resolution

**Files affected:** `data/kaggle_automation.db`, `data/accounts/*`

---

## 3. SQL Injection Fix

**File:** `app/database.py`

**Why:** `update_run_telegram_flag()` used an f-string to interpolate the column name directly into SQL:
```python
# BEFORE (vulnerable):
conn.execute(f"UPDATE runs SET {flag_name} = ? WHERE id = ?", (value, run_id))
```

Although callers only passed hardcoded values, this pattern allows SQL injection if `flag_name` is ever user-controlled.

**Fix:** Added an allowlist validation:
```python
# AFTER (safe):
allowed_flags = {"telegram_notified_start", "telegram_notified_11h",
                 "telegram_notified_12h", "telegram_notified_end"}
if flag_name not in allowed_flags:
    raise ValueError(f"Invalid flag name: {flag_name}")
conn.execute(f"UPDATE runs SET {flag_name} = ? WHERE id = ?", (value, run_id))
```

---

## 4. Fetch Quota Error Handling

**File:** `app/services/account_manager.py`

**Why:** `fetch_quota()` called `kaggle quota -v` but had no error handling for CLI failures. On this system, the command returned exit code 1 with `"not enough values to unpack (expected 2, got 1)"`. The code tried to parse empty output and returned all-zeros silently.

**Fix:**
- Added `err_str` capture alongside `out_str`
- Logs a warning when `proc.returncode != 0`
- Wrapped CSV parsing in try/except to catch malformed output
- Added `"error"` field to the summary dict when the CLI fails

---

## 5. Temporary Directory Cleanup

**File:** `app/services/account_manager.py`

**Why:** `fetch_username_for_key()` created temp directories under `data/accounts/{temp_id}/` for CLI auth, but never cleaned them up after the real username was resolved. This left behind dozens of orphaned directories.

**Fix:**
- Added `_cleanup_temp_dir()` method that removes the temp directory after username resolution
- Called it in `add_account()` after `fetch_username_for_key()` returns

---

## 6. Windows Encoding Fix (PYTHONIOENCODING)

**File:** `app/services/account_manager.py`

**Why:** The kaggle CLI on Windows outputs UTF-8 text, but the Windows console uses cp1252 encoding. This caused `kaggle kernels logs` to crash with:
```
'charmap' codec can't encode characters in position 540-579
```
The result: remote log fetching returned empty strings, and the live log stream silently dropped output.

**Fix:** Added `PYTHONIOENCODING=utf-8` to the subprocess environment in `get_account_env()`:
```python
env["PYTHONIOENCODING"] = "utf-8"
```

**Verified:** After the fix, `kaggle kernels logs` returns 12,466 bytes of output with exit code 0.

---

## 7. Kaggle CLI Path Discovery

**File:** `app/config.py` (already present from pre-session changes)

**Why:** The kaggle binary lives at `.venv/Scripts/kaggle.exe` on Windows, not on the system PATH. All CLI calls failed with "command not found" when using bare `"kaggle"`.

**How it works:** `get_kaggle_cli_path()` searches in order:
1. Current Python venv (`sys.prefix/Scripts/kaggle.exe`)
2. Project root `.venv/` directory
3. System PATH via `shutil.which()`
4. Falls back to `KAGGLE_CONFIG_DIR` env var or `"kaggle"`

**Already applied before this session** — this was part of the initial git diff. Confirmed working.

---

## 8. Accelerator Mapping Fix

**File:** `app/services/kaggle_service.py`

**Why:** The frontend sends user-friendly names like `nvidia-tesla-t4-x2`, but Kaggle's API requires specific `machine_shape` enum values like `NvidiaTeslaT4`. The old code passed the raw frontend value directly to `--accelerator`, and Kaggle silently fell back to P100 (the default GPU).

**Evidence:** The GPU Check test script reported `GPU 0: Tesla P100-PCIE-16GB` when `nvidia-tesla-t4-x2` was requested.

**Fix:** Added `ACCELERATOR_MAP` dictionary and `resolve_accelerator()` method. Also added `machine_shape` to the kernel-metadata.json so both the CLI flag and metadata agree.

**Verified:** After fix, the same test script reported `GPU count: 2`, `GPU 0: Tesla T4`, `GPU 1: Tesla T4`.

---

## 9. Full Accelerator Map

**File:** `app/services/kaggle_service.py`

**Why:** After the Kaggle CLI docs were provided, the map was expanded to cover all 12 valid accelerator types.

**Complete mapping (30 input variants → 12 API values):**

| Input variants | Kaggle API value | Notes |
|---|---|---|
| `nvidia-tesla-t4-x2`, `t4`, `t4-x2`, `gpu-tesla-t4-x2` | `NvidiaTeslaT4` | Default — gives 2x T4 |
| `nvidia-tesla-t4-highmem`, `t4-highmem` | `NvidiaTeslaT4Highmem` | High-memory T4 |
| `nvidia-tesla-p100`, `p100`, `gpu-p100` | `NvidiaTeslaP100` | ⚠️ Broken with PyTorch cu128 |
| `nvidia-a100`, `a100` | `NvidiaTeslaA100` | A100 |
| `nvidia-l4`, `l4` | `NvidiaL4` | L4 |
| `nvidia-l4-x1`, `l4x1` | `NvidiaL4X1` | L4 single |
| `nvidia-h100`, `h100` | `NvidiaH100` | H100 |
| `nvidia-rtx-pro-6000`, `rtx-pro-6000` | `NvidiaRtxPro6000` | RTX Pro 6000 |
| `v3-8`, `tpu-v3-8` | `TpuV38` | TPU v3-8 |
| `tpu1vm-v3-8`, `tpu1vmv38` | `Tpu1VmV38` | TPU v1vm v3-8 |
| `tpu-v5e-8`, `tpu-v5e8` | `TpuV5E8` | TPU v5e-8 |
| `tpu-v6e-8`, `tpu-v6e8` | `TpuV6E8` | TPU v6e-8 |

---

## 10. Frontend Accelerator Dropdown Update

**File:** `app/templates/index.html`

**Why:** The original dropdown only had 3 options (CPU, T4 x2, TPU v3-8). Updated to include all popular GPU/TPU options.

**New dropdown options (both Single Runner and Distributed):**
- Default / CPU
- T4 GPU x 2 (High Performance) — *default*
- T4 GPU High Memory
- A100 GPU
- L4 GPU
- H100 GPU
- TPU VM v3-8
- TPU v5e-8
- TPU v6e-8

---

## 11. 409 Conflict Retry Logic

**File:** `app/services/kaggle_service.py`

**Why:** In production, pushing notebook scripts returned `409 Client Error: Conflict` from Kaggle's API. This happens when:
1. A kernel push is attempted while the previous version is still starting (spinning up GPU)
2. Too many rapid pushes to the same account
3. Pushing while a kernel is in a transitional state

**Fix — `push_kernel()`:**
- Up to **4 retries** with exponential backoff: 5s → 10s → 20s → 40s
- Detects 409 in stderr and retries automatically
- Each retry is logged to both the run log file and server logger

**Fix — `stop_kernel()`:**
- Up to **3 retries** with 5s delay between attempts
- Same 409 detection logic

---

## 12. Kaggle Package Upgrade

**Command:** `uv pip install --upgrade kaggle`

**Version change:** `kaggle==2.2.2` → `kaggle==2.2.4`

**Also upgraded:**
- `kagglesdk`: 0.1.30 → 0.1.37
- `tokenizers`: (needed for new transformers)
- `certifi`, `charset-normalizer`, `idna`, `protobuf`, etc.

---

## 13. Script Fixes (kaggle_batch_inference_task_a.py)

**File:** `C:/Users/rjaya/Desktop/fitcheck-scraping/final_process/kaggle_batch_inference_task_a.py`

### 13a. Wrong Model Class Import
**Before:** Tried `from transformers import Qwen2_5_VLForConditionalGeneration` — wrong class for `Qwen/Qwen3.6-35B-A3B`.
**After:** Uses `AutoModelForCausalLM` with `trust_remote_code=True` which auto-selects the correct class.

### 13b. Broken Shard Config Fallback
**Before:**
```python
START_INDEX = globals().get('START_INDEX', int(os.environ.get('START_INDEX', 0)) if 'START_INDEX' in os.environ else None)
```
This was convoluted and broke when `START_INDEX` was set to `0` (falsy).

**After:** Clean `_get_shard_var()` helper:
```python
def _get_shard_var(name, default=None):
    val = globals().get(name)
    if val is not None:
        return val
    env_val = os.environ.get(name)
    if env_val is not None:
        return type(default)(env_val) if default is not None else env_val
    return default
```

### 13c. No GPU Memory Guard
**Before:** Model loaded blindly — if OOM, unhelpful crash.
**After:** Checks total VRAM before loading. Exits with clear error if no GPU. Warns if <28 GB.

### 13d. Silent `os.system` Failures
**Before:** `os.system("pip install ...")` silently swallowed errors.
**After:** `subprocess.run()` + `run_cmd()` helper that checks return codes, shows stdout/stderr on failure, and calls `sys.exit(1)`.

### 13e. Aggressive `pip install --upgrade transformers` Breaking Torch
**Before:** `pip install --upgrade transformers` pulled in a new torch that broke FSDP imports.
**After:**
```python
# Install transformers from source (for Qwen3 MoE support)
# with --no-deps to avoid upgrading torch
pip install --no-deps git+https://github.com/huggingface/transformers.git
# Then install its deps separately
pip install tokenizers>=0.23.1 safetensors>=0.8.0 huggingface_hub>=0.30.0 ...
```

### 13f. Added `uv pip` Support
**After:** Script detects `uv` availability and falls back to `pip`:
```python
PIP = "uv pip install" if subprocess.run("uv --version", ...).returncode == 0 else "pip install -q"
```

---

## 14. End-to-End Verification

### Test Results

| Test | Result |
|---|---|
| Single run push | ✅ Queued → Running → Complete |
| Distributed run push (1 account, 33772 items) | ✅ Queued → Running → Complete |
| Accelerator: `nvidia-tesla-t4-x2` | ✅ Got `NvidiaTeslaT4` → 2x Tesla T4 GPUs |
| Live log streaming | ✅ `PYTHONIOENCODING=utf-8` fixed encoding |
| Status monitoring (`kaggle kernels status`) | ✅ Detects `queued → running → error → complete` |
| Telegram notification | ✅ `telegram_notified_start` flag set |
| Unit tests (`test_automation.py`) | ✅ 3/3 pass |

### Verified Working API Endpoints
- `GET /api/health` — ✅
- `GET /api/accounts` — ✅ Returns clean `@darkzone16` account
- `POST /api/accounts` — ✅ Account creation with real username resolution
- `POST /api/runs/launch-json` — ✅ Pushes notebook with correct accelerator
- `GET /api/runs/active` — ✅ Lists running sessions
- `GET /api/runs/{id}/refresh-status` — ✅ Queries Kaggle CLI
- `GET /api/runs/{id}/logs?fetch_remote=true` — ✅ Fetches remote logs
- `POST /api/distributed/launch-json` — ✅ Distributes workload with shard injection
- `POST /api/runs/{id}/stop` — ✅ Pushes stop stub with retry

---

## Summary of All Files Modified

| File | Changes |
|---|---|
| `app/config.py` | Pre-existing: `get_kaggle_cli_path()` function |
| `app/database.py` | SQL injection allowlist in `update_run_telegram_flag()` |
| `app/main.py` | Cleaned up startup flow (removed `fix_fake_usernames` call) |
| `app/services/account_manager.py` | `PYTHONIOENCODING=utf-8`, quota error handling, temp dir cleanup, removed dead code |
| `app/services/kaggle_service.py` | Accelerator mapping (30 inputs → 12 API values), `machine_shape` in metadata, 409 retry logic (4x push + 3x stop) |
| `app/templates/index.html` | Updated accelerator dropdowns (3 → 9 options) |
| `kaggle_batch_inference_task_a.py` | Fixed model class, shard fallback, GPU guard, subprocess errors, uv support, dependency install strategy |

## Data Cleanup

| Item | Before | After |
|---|---|---|
| Accounts in DB | 21 (20 fake + 1 test) | 1 (`@darkzone16`) |
| Account directories | 65+ | 1 |
| Fake usernames | `kaggle_user_*` pattern | None |
| Test data | Orphaned runs & accounts | Removed |
