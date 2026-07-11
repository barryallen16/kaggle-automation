# Kaggle Automation Platform — Changelog

> Complete record of all changes made during development.
> **Session 1** (August 22, 2026): initial diagnosis through infrastructure fixes.
> **Session 2** (August 22–23, 2026): security audit & hardening, authentication, branding, Telegram DM alerts, and full Kaggle verification of the inference pipeline.
> **Session 3** (August 23, 2026): dispatch failure root-caused & fixed; single + distributed workloads verified end-to-end on real Kaggle accounts.

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
13. [Script Fixes (kaggle_batch_inference_task_a.py) — Session 1](#13-script-fixes)
14. [End-to-End Verification — Session 1](#14-end-to-end-verification)

**Session 2 (August 22–23, 2026):**

15. [Security Audit — Critical Fixes](#15-security-audit--critical-fixes)
16. [Correctness Fixes](#16-correctness-fixes)
17. [Test Suite Isolation](#17-test-suite-isolation)
18. [Streaming ZIP Downloads](#18-streaming-zip-downloads)
19. [requirements.txt Trimmed](#19-requirestxt-trimmed)
20. [CDN Dependencies Pinned](#20-cdn-dependencies-pinned)
21. [Branding: Geist Pixel Fonts, Logo & Favicon](#21-branding-geist-pixel-fonts-logo--favicon)
22. [Authentication (Shared-Secret Cookie)](#22-authentication-shared-secret-cookie)
23. [UI/UX Consistency Fixes](#23-uiux-consistency-fixes)
24. [Telegram Alerts → Direct Messages via User ID](#24-telegram-alerts--direct-messages-via-user-id)
25. [Inference Script: Full Kaggle Verification (v1→v10)](#25-inference-script-full-kaggle-verification-v1v10)
26. [Local Test Harness for Inference Script](#26-local-test-harness-for-inference-script)

**Session 3 (August 23, 2026):**

27. [Dispatch Failure: CLI Auth Channel Fix (root cause)](#27-dispatch-failure-cli-auth-channel-fix-root-cause)
28. [Notebook Normalization: kernelspec + raw-code wrapping](#28-notebook-normalization-kernelspec--raw-code-wrapping)
29. [Slug/Title Mismatch 409 Fix](#29-slugtitle-mismatch-409-fix)
30. [Quota CSV Schema Update + Output Download Flag Fix](#30-quota-csv-schema-update--output-download-flag-fix)
31. [Session 3 End-to-End Verification](#31-session-3-end-to-end-verification)

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
> ⚠️ **Superseded in Session 2 (§25):** `AutoModelForCausalLM` turned out to be wrong too — the model is an
> image-text-to-text architecture (`Qwen3_5MoeForConditionalGeneration`) and now loads via `AutoModelForImageTextToText`.
> The production model was also switched to `Qwen/Qwen3.6-27B`.

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

# Session 2 — August 22–23, 2026

## 15. Security Audit — Critical Fixes

A full audit of all ~2,200 lines found 19 flaws. Critical ones fixed and verified:

### Path Traversal (verified exploitable, then fixed)
**File:** `app/routers/files.py`, `app/routers/logs.py`
- `GET /api/runs/{run_id}/files/download/{filename}` built paths with zero sanitization — a crafted request could serve **any file on disk**, including `.env` (all API keys + Telegram token). Proven working with a live exploit before fixing.
- **Fix:** run-id regex allowlist (`^[A-Za-z0-9_\-]+$`) + resolve-and-verify containment inside the run's output dir. Re-verified over live HTTP: traversal attempts now return `400`.

### API Key Leak
**File:** `app/routers/accounts.py`
- `POST /api/accounts/refresh` returned raw DB rows including **full plaintext api_key** for every account (the list endpoint masked keys; refresh didn't).
- **Fix:** shared `_sanitize_account()` masking helper used by both endpoints.

### LAN Exposure & CORS
**Files:** `run.py`, `app/main.py`
- Server bound `0.0.0.0` with zero auth — anyone on the network could launch kernels, delete accounts, read logs.
- CORS was `allow_origins=["*"]` **plus** `allow_credentials=True` (invalid combo).
- **Fix:** loopback bind by default (`APP_HOST` env override for deliberate exposure), CORS restricted to localhost origins with credentials off.

### Stored XSS
**Files:** `app/static/js/*.js` (6 files)
- Notebook titles / usernames / kernel refs were interpolated into `innerHTML` unescaped — a malicious notebook title executed JS in the dashboard.
- **Fix:** `esc()` helper applied to every user-controlled interpolation; toasts switched to `textContent`.

---

## 16. Correctness Fixes

| Bug | File | Fix |
|---|---|---|
| Account rename corrupted credentials (`copytree` into pre-created dir → swallowed `FileExistsError`) | `account_manager.py` | `copytree(..., dirs_exist_ok=True)` + remove old dir |
| Ghost accounts: restart re-added every env key under random names (`kaggle_user_xxx`) when username couldn't be resolved | `account_manager.py` | Idempotent init (same key → reuse account) + deterministic SHA-256 fallback name |
| Runs marked failed if stderr merely contained the word "Error" | `kaggle_service.py` | Failure = non-zero exit code or explicit push error only |
| `download_outputs` ignored CLI failures → UI reported success with 0 files | `kaggle_service.py`, `files.py` | Raise on failure; pull endpoints return `502` with real error |
| Stop button always claimed success, even when the stop-push failed; also destroyed notebooks (script stub replacing `.ipynb`) | `kaggle_service.py` | Preserves kernel type via ipynb stub; marks stopped only on confirmed success |
| `except Exception` converted intended 400s into 500s; bad accounts JSON crashed | `runs.py`, `distributed.py` | `HTTPException` passthrough + JSON decode → 400; `limit` clamped 1–500 |
| Distributed workloads stuck at `"running"` forever; total failures reported as success | `workload_distributor.py`, `database.py` | Final status `dispatched/partial/failed`; rejects items < shards |
| One bad run stalled the whole monitor cycle; trial runs got 11h/12h alerts | `session_monitor.py` | Per-run isolation; long-session alerts skip trials; legacy naive-timestamp parsing handled |
| SQLite "database is locked" under concurrent writes | `database.py` | WAL journal mode + 30s busy timeout |
| Naive UTC timestamps broke JS elapsed display in non-UTC timezones; deprecated `utcnow()` | everywhere | Timezone-aware `utcnow_iso()` helper |

---

## 17. Test Suite Isolation

**Files:** `app/config.py`, `test_automation.py`
- Tests wrote directly into the production DB and `data/accounts/`.
- **Fix:** `AUTOMATION_DATA_DIR` env var redirects ALL data paths; tests use a per-run temp dir with fresh DB per test and teardown cleanup.
- Edge case found & fixed while testing: set-but-empty `AUTOMATION_DATA_DIR=""` made `Path("")` resolve to cwd — empty values now treated as unset.

---

## 18. Streaming ZIP Downloads

**File:** `app/routers/files.py`
- ZIP archives were buffered fully in RAM (OOM risk on large outputs).
- **Fix:** archive written to a temp file, streamed via `FileResponse`, deleted by `BackgroundTask` after the response. Verified live: correct contents, payload intact, temp file auto-deleted.

---

## 19. requirements.txt Trimmed

68-line full freeze (incl. unrelated `sentry-sdk`, `fastapi-cloud-cli`, `fastar`, …) reduced to the 7 real direct dependencies with known-good ranges: `fastapi`, `uvicorn[standard]`, `jinja2`, `python-dotenv`, `python-multipart`, `httpx`, `kaggle`. All ranges verified against the installed environment.

---

## 20. CDN Dependencies Pinned

`index.html`: tailwind `cdn.tailwindcss.com` → pinned `3.4.17`; lucide `@latest` → pinned `1.33.0` (exactly what `latest` resolved to — zero behavior change, no surprise majors).

---

## 21. Branding: Geist Pixel Fonts, Logo & Favicon

**Files:** `app/static/fonts/*`, `app/static/icons/*`, `css/fonts.css` (new), `custom.css`, `index.html`
- Unzipped `geist-font-v1.7.2.zip` → all 5 Geist Pixel variants (Square/Circle/Grid/Line/Triangle) + Geist + Geist Mono served locally from `/static/fonts`.
- Body text now Geist, code/terminal Geist Mono (Google Fonts CDN import removed — dashboard works fully offline).
- Display font `.font-pixel` (Square) applied to sidebar brand and page title; other variants available via utility classes.
- Unzipped `favicon_io.zip` → favicon.ico + PNG sizes + apple-touch + android-chrome wired into `<head>`; `site.webmanifest` paths corrected and themed `#0a0d14`; sidebar logo box replaced with the actual logo image.

---

## 22. Authentication (Shared-Secret Cookie)

**Files:** `app/auth.py` (new), `app/main.py`, `app/routers/logs.py`, `templates/login.html` (new), `index.html`
- `APP_AUTH_TOKEN` in `.env` enables auth; unset = disabled (local dev) with loud startup warning.
- Login page posts the token → server sets an HMAC-SHA256-signed HttpOnly `SameSite=Strict` cookie (7-day TTL). Signing key derived from the token hash, so **rotating the token invalidates all sessions instantly**.
- HTTP middleware guards everything except `/login`, `/static/*`, `/api/health`: pages redirect to login, APIs get `401`.
- **WebSockets guarded separately** (`close(1008)`) — they bypass HTTP middleware and were previously open to cross-site hijacking.
- Branded login page; Sign Out button in header (hidden when auth disabled); frontend auto-redirects to login when a session expires mid-polling; 0.6s dampener on failed logins.
- Verified live E2E: redirect flow, cookie issue/verify, tampered-cookie rejection, logout clearing, disabled-mode unchanged.

---

## 23. UI/UX Consistency Fixes

1. Accelerator dropdowns ordered differently between tabs → unified (CPU first).
2. "Elapsed / Max" showed hardcoded `/ 12h` even for 5-minute trial runs → derives from each run's `timeout_seconds`.
3. "CLI Engine Online" badge was static → now polls `/api/health` and shows red *"Kaggle CLI Not Found"* when missing.
4. Toasts could render underneath the modal (both z-50) → toast layer raised above.
5. Add-Account modal: ESC key and backdrop-click close added.
6. All three data tables wrapped in horizontal-scroll containers for narrow screens.
7. Terminal "Clear" button border/hover matched to sibling buttons.

---

## 24. Telegram Alerts → Direct Messages via User ID

**Files:** `telegram_service.py`, `settings` UI copy, `readme.md`
- Alerts are bot **direct messages to a numeric Telegram User ID** instead of channel posts (Bot API `chat_id` accepts both natively — no schema change).
- Actionable error hints added and surfaced in UI toasts:
  - `403 can't initiate conversation` → *"press START on the bot first"*
  - `chat not found` → *"get your numeric ID from @userinfobot"*
- Settings tab relabeled with built-in 2-step instructions; test-message copy rewritten for DMs.

---

## 25. Inference Script: Full Kaggle Verification (v1→v10)

**File:** `kaggle_batch_inference_task_a.py`

The script was pushed to real Kaggle GPUs ten times, each error diagnosed from logs and fixed:

| Ver | Kaggle-reported failure | Fix |
|---|---|---|
| 1 | `AutoModelForCausalLM` can't load `Qwen3_5MoeForConditionalGeneration` (image-text-to-text arch) | → `AutoModelForImageTextToText` |
| 2 | offload kwarg leaked into model `__init__` | moved inside `BitsAndBytesConfig` |
| 3–4 | disk-offloaded meta tensors crash at forward | explicit per-GPU memory budgets |
| 5 | disk spill again during dispatch save | dropped CPU-offload flag entirely |
| — | operator decision: smaller teacher | model → `Qwen/Qwen3.6-27B` (27.8B, NF4 ≈ 17 GB) |
| 7–8 | CUDA OOM inside SDPA attention | budget → 13.5 GiB/GPU (activation headroom) |
| 9 | COMPLETE but output was chain-of-thought truncated at 256 tokens | thinking-mode handling |
| **10** | ✅ **COMPLETE with clean structured JSON** | see below |

Static fixes carried into the script:
- Shard vars coerced to int from env (raw strings crashed slicing/comparisons)
- `uv pip install --system` (Kaggle has no venv); `--no-deps` strategy protecting pre-installed torch
- `/kaggle/working` fallback to CWD; fetch retry backoff; preview blank-line guard
- OOM root cause: 4 images/item × default pixel budgets → thousands of visual tokens. Fixed via 768 px download cap + processor `min_pixels/max_pixels=768*28*28`
- Thinking mode disabled (`enable_thinking=False`), `max_new_tokens=768`, `<think>` stripping in JSON extraction
- Fast downloads: `hf_transfer` enabled when available (multi-connection HF pulls; aria2c/GGUF considered and rejected — bottleneck was VRAM, not bandwidth)

**Final verified state:** smoke kernel `fitcheck-taska-smoke-2-items` v10 — 2/2 items labeled with clean JSON (`category`/`gender`/`occasion`/`description`) in 2.77 min inference time (~14 min wall incl. model download). Model download via hf_transfer confirmed active.

---

## 26. Local Test Harness for Inference Script

**File:** `test_inference_script.py` (new)
- Executes the real script end-to-end with mocked torch/transformers/qwen_vl_utils/PIL, patched subprocess (installs recorded), REAL `requests` against a local HTTP image server, real file I/O.
- 8 tests: compile+install flags, model-class regression guard, single-shard pipeline (success/fail counting, output schema, JSON extraction matrix), resume idempotency, exact multi-shard partition (zero overlap/duplication), env-var int coercion, JSON extractor edge cases, no-GPU exit path.
- Suite total across project: **11/11 green**.

---

## Summary of All Files Modified

| File | Changes |
|---|---|
| `app/config.py` | Session 1: `get_kaggle_cli_path()`. Session 2: `AUTOMATION_DATA_DIR` override (empty-safe), `APP_AUTH_TOKEN` |
| `app/database.py` | SQL injection allowlist; WAL + busy timeout; tz-aware `utcnow_iso()`; `update_workload_status()` |
| `app/main.py` | Cleaned startup flow; auth middleware + login/logout routes; CLI-aware health check; CORS lockdown |
| `app/auth.py` | **NEW** — HMAC-signed session cookie helpers, WS/HTTP guards |
| `app/routers/files.py` | Path traversal fix (verified exploit), 502 on download failures, streaming ZIP via temp file |
| `app/routers/logs.py` | Log-path traversal guard, WebSocket auth guard (close 1008) |
| `app/routers/accounts.py` | API-key masking on refresh endpoints (leak fix) |
| `app/routers/runs.py`, `distributed.py` | HTTPException passthrough, 400 validation, limit clamp |
| `app/services/account_manager.py` | PYTHONIOENCODING, quota error handling, temp cleanup, rename copytree fix, idempotent env init, deterministic fallback names, add-account lock |
| `app/services/kaggle_service.py` | Accelerator map, machine_shape, 409 retries, false-error fix, download rc check, type-preserving honest stop_kernel, utcnow_iso |
| `app/services/session_monitor.py` | Per-run isolation, trial-aware alerts, legacy timestamp parsing |
| `app/services/workload_distributor.py` | Workload finalization, degenerate-split guard |
| `app/services/telegram_service.py` | HTML escaping, User-ID DM orientation + actionable error hints |
| `app/templates/index.html` | Accelerator dropdowns, favicons/logo, pixel fonts, Sign Out, modal ESC/backdrop, toast z-order, table scroll wrappers |
| `app/templates/login.html` | **NEW** — branded login page |
| `app/static/css/fonts.css`, `fonts/*`, `icons/*` | **NEW** — local Geist/Geist Mono/Geist Pixel + favicon pack |
| `app/static/js/*.js` (6 files) | XSS escaping (`esc()`), timeout-derived elapsed display, CLI indicator truthfulness, 401 redirect |
| `run.py` | Loopback bind by default (`APP_HOST`/`APP_PORT` overrides) |
| `requirements.txt` | 68-line freeze → 7 direct deps with ranges |
| `test_automation.py` | Full isolation via temp data dir |
| `test_inference_script.py` | **NEW** — 8-test mocked end-to-end harness |
| `kaggle_batch_inference_task_a.py` | Model class fix, Qwen3.6-27B, memory budgets, OOM/pixel budgets, thinking-mode off, hf_transfer, shard/env/install/path fixes |
| `readme.md` | Auth + production notes, Telegram User-ID docs |

## Data Cleanup

| Item | Before | After |
|---|---|---|
| Accounts in DB | 21 (20 fake + 1 test) | 0 (production keys added at startup) |
| Account directories | 65+ | Recreated per-account at startup |
| Fake usernames | `kaggle_user_*` pattern | Deterministic fallback only if unresolvable |
| Test data | Orphaned runs & accounts | Removed; tests isolated to temp dirs |

---

# Session 3 — August 23, 2026

## 27. Dispatch Failure: CLI Auth Channel Fix (root cause)

**Symptom:** Every launch (single and distributed) returned `success: false` with
`"Authentication required to call the Kaggle API."` — nothing was ever dispatched.

**Root cause:** `kaggle` CLI 2.2.4 / `kagglesdk` 0.1.37 authenticates access tokens from
ONLY two places: the `KAGGLE_API_TOKEN` env var or `~/.kaggle/access_token`.
The platform wrote each account's token to `$KAGGLE_CONFIG_DIR/access_token`, which this
CLI version never reads (it only honors `KAGGLE_CONFIG_DIR` for legacy `kaggle.json`).
Result: every subprocess ran unauthenticated → every dispatch errored, quota fetches
returned junk CSV rows parsed as "accounts", and usernames fell back to `kaggle_<hash>`.

**Fix (`app/services/account_manager.py`):**
- `get_account_env()` now reads the account's `access_token` file and exports it as
  `KAGGLE_API_TOKEN` in the subprocess environment (skipped for JSON `kaggle.json`
  credentials). Per-account isolation is fully preserved — `~/.kaggle` is never touched.
- Side effect fixed automatically: username resolution at startup now works again;
  accounts re-register with their real Kaggle usernames.

## 28. Notebook Normalization: kernelspec + raw-code wrapping

**File:** `app/services/kaggle_service.py`

**Symptom:** Pushes succeeded but kernels died on Kaggle with
`ValueError: No kernel name found in notebook and no override provided.`

**Root cause:** Two gaps:
1. Notebooks without `metadata.kernelspec` are rejected by Kaggle's papermill runner.
2. The UI pastes raw Python with default filename `notebook.ipynb`; raw text pushed as a
   "notebook" crashes the CLI's `json.loads` during push.

**Fix:** New `ensure_executable_notebook()` normalizer applied to every `.ipynb` payload:
- Injects a default `python3` kernelspec when missing (also added to the stop-kernel stub).
- Wraps raw Python source into a valid single-cell notebook when the content is not JSON.

## 29. Slug/Title Mismatch 409 Fix

**File:** `app/services/kaggle_service.py`

**Symptom:** Re-launching any run hit persistent `409 Client Error: Conflict` on
SaveKernel; all 4 retry attempts failed even minutes after the previous run ended.

**Diagnosis (verified against live API):** Kaggle keys notebooks by the slugified TITLE.
The old `sanitize_slug()` appended a random hex suffix to the metadata `id`
(`title-a1b2`), so the id pointed at a non-existent kernel while the title mapped to the
existing one → every subsequent push conflicted. (Create tolerated the mismatch once;
updates never do.)

**Fix:** `sanitize_slug(title)` now derives the slug exactly like Kaggle does from the
(truncated ≤50 char) title — no suffix. Same-title launches become clean new versions of
the same kernel; distributed shard titles are already unique via `[Shard i/n]`.

## 30. Quota CSV Schema Update + Output Download Flag Fix

**Files:** `app/services/account_manager.py`, `app/services/kaggle_service.py`
- `kaggle quota -v` now emits `resource,used,remaining,total,refreshAt` (e.g.
  `GPU,2.58h,27.42h,30.00h,...`). The parser expected the old
  `accelerator/used/limit` schema, so meters always showed zeros. Rewritten to handle
  both schemas with tolerant hour-string parsing (`"2.58h"`).
- `download_outputs()` passed the kernel ref as `-o <ref>`, but `-o` is a boolean force
  flag in CLI ≥ 2.x — kernel ref must be positional. Fixed to
  `kernels output <ref> -p <dir> -o`; artifact pulls verified working.
- GPU/TPU boolean derivation now keys off the resolved accelerator map, so
  `nvidia-l4`/`a100`/`h100` correctly set `enable_gpu=true` (they contain neither
  `"gpu"` nor `"t4"` substrings).

## 31. Session 3 End-to-End Verification

| Test | Result |
|---|---|
| Single run push (`jayadithyx16`, T4 x2 trial) | ✅ Queued → Running → **COMPLETE**, 2x Tesla T4 allocated |
| Distributed push (2 accounts, 100 items) | ✅ status `dispatched`, shards [0,50) + [50,100) |
| Distributed execution on Kaggle | ✅ Both shards COMPLETE, 50 items each |
| Shard partition integrity | ✅ Zero overlap; union = exactly 0..99 |
| Artifact pull (`files/pull`) | ✅ `shard_result.json` downloaded & verified correct |
| Stop endpoint (stub kernelspec path) | ✅ Stop push accepted, run marked stopped |
| Quota parsing (new CSV schema) | ✅ GPU 2.58h/30h = 8.6% shown correctly |
| Real username resolution at startup | ✅ `jayadithyx16` + `darkzone16` (was `kaggle_<hash>`) |
| Unit suites (`test_automation.py`, `test_inference_script.py`) | ✅ 3/3 + 8/8 green |

**Note:** data/ was wiped before verification (polluted DB held fake accounts from the
broken-auth era); `.env` keys auto re-registered cleanly on startup.

---

# Session 4 — August 23, 2026 (UI/UX Overhaul + Auth Loop Fix)

## 32. Back-Button Login Loop Fix

**Symptom:** Pressing the browser back button after signing in always landed on the
login page again, demanding re-authentication.

**Root cause (two parts):**
1. `GET /login` always rendered the form, even for already-authenticated users. The
   history stack was `/ → /login?next=/ → /`, so "back" hit the stale login entry.
2. No `Cache-Control` headers meant browsers could replay cached auth pages/redirects.

**Fix (`app/main.py`):**
- Authenticated users hitting `/login` are now 303-redirected to `next` (or `/`);
  open redirects blocked (`//host`, scheme-relative) — falls back to `/`.
- Middleware sets `Cache-Control: no-store` on every non-static response so cached
  pages can never resurrect a stale login state. Static assets keep normal caching.

**Related fix (`app/static/js/app.js`):** the session-expiry check was dead code —
`fetch(...).then(r => r.json())` consumed the response before `r.status === 401`
could be inspected, so expired sessions never redirected to login. New
`fetchAuthedJson()` helper checks status first and bounces to `/login` on 401.

## 33. Geist Pixel as Site-Wide Default Font

**Files:** `app/static/css/custom.css`, `app/templates/index.html`, `login.html`

Geist Pixel Square is now the default font for ALL text (was body = plain Geist,
Pixel only on headings). Tailwind's `fontFamily.sans` maps to
`['Geist Pixel Square', 'Geist', 'monospace']` and `mono` to `'Geist Mono'`, so
every utility class renders with local fonts. Code/terminal stays Geist Mono.

## 34. Pixel Icons (Pixelarticons) Replace Lucide

Streamline's Pixel pack is behind an account/license wall (HTTP 429 + gated
downloads), so the dashboard now bundles **Pixelarticons v2.4.1 (MIT)** instead:
true pixel-art glyphs, monochrome via `fill="currentColor"`, rendered with
`shape-rendering: crispEdges`.

- New `app/static/js/pixel-icons.js`: all 41 needed icons inlined as SVG strings;
  exposes `refreshIcons(root)` which replaces any `<i data-lucide="name"></i>` while
  preserving its classes (`w-4 h-4 text-cyan-400 animate-spin`, etc.). Idempotent.
- Raw SVGs also stored at `app/static/icons/pixel/<name>.svg` for inspection/swaps;
  icon names are keyed by the original lucide names, so no markup changes were needed.
- Lucide CDN script removed from both templates; all 9 `lucide.createIcons()`
  call sites across templates + 6 JS modules switched to `refreshIcons()`.
- Verified: zero missing icons across all templates/JS; runtime replacement tested
  under node.

## 35. Flat Theme — All Gradients & Glows Removed

The blue-gradient look read as generic AI slop. Everything is flat solid colors now:

| Before | After |
|---|---|
| Launch buttons: `bg-gradient-to-r from-cyan-500 to-blue-600` + glow shadows | Solid `bg-cyan-600 hover:bg-cyan-500`, no shadow |
| Distributed button: purple→indigo gradient | Solid `bg-purple-600 hover:bg-purple-500` |
| Quota bars: cyan→blue / purple→indigo gradients | Flat `bg-cyan-400` / `bg-purple-400` |
| Account avatar tile: gradient fill | Flat `bg-cyan-500/10` |
| Active nav tab: linear-gradient background | Flat `rgba(0,242,254,0.08)` |
| Login page: two blurred ambient glow blobs | Removed |
| Logo tiles: colored glow shadows | Plain border only |

Verified: `grep -c gradient` returns 0 across served HTML, templates, JS and CSS.

## Session 4 Verification

| Check | Result |
|---|---|
| Unauth `/` → redirect `/login?next=/` | ✅ 303 |
| Login POST issues signed cookie | ✅ 303 → `/` |
| Authed GET `/login` (back button) | ✅ 303 → `/` — never re-prompts |
| `next=/terminal` honored, `//evil.com` blocked | ✅ |
| `Cache-Control: no-store` on pages; static caching preserved | ✅ |
| Lucide CDN removed; pixel-icons.js serves 41 icons | ✅ |
| All used icon names covered by bundle | ✅ 38/38 |
| Gradient count in UI sources/served HTML | ✅ 0 |
| Unit suites + API smoke | ✅ green |

## 36. Run Catalog "Logs" Button + Back-Button Navigation + Logo Polish

**Run Catalog Logs button did nothing visible (`app/static/js/terminal.js`):**
`viewLogsForRun()` connected the WebSocket stream but never left the current tab -
the terminal lived inside a hidden section, so clicking Logs (or launching a run)
updated invisible state only. It now calls `switchTab('terminal')` first, matching
the Outputs button's behavior.

**Back button exited the app to `/login?next=/`:** the entire dashboard is a single
page, so tab clicks created no browser history - Back always fell through to the
stale pre-login entry. Fix (`app/static/js/app.js`):
- `switchTab()` now pushes `/?tab=<id>` via the History API; Back/Forward move
  between app views instead of leaving the site.
- Deep-linking: `/?tab=history` (or `#history`) opens that tab directly;
  `popstate` restores views without reloading.
- Defense-in-depth for the login loop: login POST now honors `?next=` (validated),
  and `login.html` force-reloads itself when restored from the back/forward cache
  (`pageshow.persisted`), so a stale cached form can never appear.

**Logo borders removed:** dropped the cyan ring around the sidebar logo and the
login-page logo per design feedback.

## 37. Logs Button Reliability (HTTP-first) + Action Button Alignment

**Logs still blank behind the public proxy:** clicking Logs relied entirely on the
WebSocket (`kernels logs -f`) for ALL output. Through `https://kabila-aws.isroot.in`
the WS upgrade can fail (proxy without upgrade forwarding), leaving only
"Connecting..." forever.

**Fix (`terminal.js`, `logs.py`):**
- Clicking Logs now FIRST loads the stored log file over plain HTTP
  (`GET /api/runs/{id}/logs`) - always works, proxy or not - and renders it.
- Live WebSocket attaches ONLY for queued/running runs, with a new
  `?skip_initial=1` param so the server doesn't re-send the whole file (no more
  duplicated output after the HTTP load).
- `connectTerminalSocket` is append-only now (never wipes loaded logs), and its
  error state says exactly what to do ("use Fetch Full Kaggle Log").
- Verified via scripted WS clients: normal connect dumps the file; skip_initial=1
  sends nothing until live lines/keepalives.

**Action buttons touching/misaligned in Run Catalog:** the actions cell used
`text-right space-x-2`; when the column got narrow the buttons wrapped vertically
with zero vertical gap. Replaced with a flex container
(`flex items-center justify-end flex-wrap gap-2`) in both the Run Catalog and the
Dashboard active-runs table - consistent spacing horizontally AND when wrapped.

## 38. Why Live Logging Stalled - Streamer Hardened

**Symptom:** mid-run, live log updates stopped forever even though the kernel kept
running; the terminal just showed keepalive silence.

**Root causes (both real bugs in the old streamer):**
1. **stderr never drained:** the follower CLI ran with `stderr=PIPE` that nobody
   read. Once ~64KB of stderr accumulated, the OS pipe buffer filled and the CLI
   *blocked on write* - stdout went permanently quiet while everything looked alive.
2. **No recovery path:** if the follower exited (throttle/error/restart), streaming
   was dead for good. Server restarts also orphaned all streamers silently.

**Fix (`kaggle_service.py`, `logs.py`):**
- Follower rewritten as a supervised loop: stderr drained concurrently (no more
  pipe deadlock), exits trigger reconnect with backoff (3s..60s), productive
  attempts reset the backoff, and terminal kernel states (complete/error/stopped)
  end the loop cleanly instead of retrying forever.
- Poll interval raised 3s -> 10s (Kaggle-API friendly for 12h sessions).
- New `ensure_log_stream()`: opening the Logs view on an active run revives a dead
  follower automatically - self-healing after crashes/restarts without re-pushing.
- Verified live against the running inference job: WS connect revived the producer,
  log file resumed growing (+43KB), live tail flowing.

## 39. Silent Kaggle-Side Logs During Long Inference Loops

**Symptom:** Kaggle's own notebook log page showed nothing after entry #28
(~14 min mark) while the kernel kept RUNNING for 50+ minutes. Dashboard streaming
was fine - it faithfully mirrors what Kaggle exposes.

**Diagnosis:** kernel alive (`KernelWorkerStatus.RUNNING`). The inference loop
communicated only via tqdm `\r` bar updates (~100 bytes per 85s item). Under
papermill, stdout is block-buffered (~4-8KB) and `\r` writes never terminate a
line, so neither Python's buffer nor Kaggle's log ingestion flushed an entry -
the same effect that made "Loading weights" appear as one blob only after
finishing. Byte-heavy startup phases flushed immediately, masking the issue.

**Fix (`kaggle_batch_inference_task_a.py`):**
- `sys.stdout/stderr.reconfigure(line_buffering=True)` right after imports -
  every print reaches the log viewer immediately even when piped.
- One newline-terminated `[PROGRESS i/N] starting id=... | ok=.. fail=.. | HH:MM:SS`
  line per iteration (flush=True): guaranteed visible heartbeat at ~85s/item,
  plus live ok/fail counters.
- Verified: all 8 harness tests pass; script compiles.

**Note:** the in-flight run (pushed before this fix) keeps the old behavior - its
page stays quiet until its buffers flush or the session ends. All future pushes
log every item.

## 40. max_new_tokens Cut (384) + Fine-Tuning Dataset Guards

Teacher outputs feed a smaller student model, so dataset purity matters more
than raw recall:

- `MAX_NEW_TOKENS = 768 -> 384`: thinking mode is off and labels are structured
  JSON, so clean output lands well under the old cap; worst-case decode time
  roughly halves.
- **Truncation/unparseable guard**: `extract_json_response` returns
  `{"raw_output": ...}` instead of raising, so cut-off generations previously
  entered the dataset silently as garbage. Now: parse failure OR hitting the
  token cap -> item skipped, counted as failed, retried via resume on rerun.
- `[PROGRESS]` lines now include completion-token avg/max, so the cap can be
  re-tuned from real data.
- Harness updated to the new contract (unparseable = skipped) + new regression
  test: valid-JSON-but-truncated generations never reach the output file.
  Suites: 9/9 inference + 3/3 automation green.
