# Progress — Kaggle Multi-Account Automation Platform

> Living status document. Full engineering detail lives in `CHANGES.md`
> (§1–§45+). This file tracks **where we are right now**: approach, completed
> work, and the failure currently being worked.

---

## 1. Status Snapshot (2026-08-23 ~17:10 UTC)

| Item | State |
|---|---|
| Server | RUNNING - 127.0.0.1:8000, latest code; public via https://kabila-aws.isroot.in |
| Accounts | 2 registered w/ real usernames + valid tokens: darkzone16, jayadithyx16 |
| HF_TOKEN | Read-scoped token in `.env`, auto-injected into kernels (verified live) |
| Test suites | test_distributor 8/8 - test_automation 3/3 - test_inference_script 9/9 |
| **Active job** | ✅ **NONE - launch #5 COMPLETE: all 4 shards, 200/200 items, zero overlap** |
| Last goal | Distributed test DONE (CHANGES.md §45); next: scale accounts or micro-batching |

---

## 2. What The Platform Is

FastAPI dashboard orchestrating multiple Kaggle accounts: pushes notebooks/scripts
via kaggle CLI v2.2.4, streams logs over WebSockets, monitors 12-hour sessions
with Telegram alerts, distributes large jobs as shards across accounts, and pulls
output artifacts back. Each account: 30h/week GPU quota, hard cap of **2
concurrent batch-GPU sessions** (Kaggle-enforced server-side).

---

## 3. Approach / How Dispatch Works

1. **Auth**: each CLI subprocess gets its account token via KAGGLE_API_TOKEN env
   var (kaggle >=2.x ignores `$KAGGLE_CONFIG_DIR/access_token`).
2. **Identity**: Kaggle keys kernels by slugified TITLE. Same title = new version
   of same kernel. Launcher blocks duplicate same-title launches while active.
3. **Normalization**: .ipynb payloads get python3 kernelspec injected; raw Python
   mislabeled as .ipynb gets wrapped into a valid notebook.
4. **Distribution**: `sessions_per_account` (default 2) expands accounts into
   runners; items split evenly across R runners; unique title "[Shard i/R]" per
   runner; atomic pre-flight before any workload row is created.
5. **Env injection**: keys from .env (HF_TOKEN) + optional request env_vars are
   prepended as os.environ preamble before user code. READ-scoped HF token is
   sufficient (public downloads only).
6. **Logging**: follower streams `kernels logs -f` to file + WS subscribers;
   stderr drained, reconnect/backoff, self-healing via ensure_log_stream(). The
   inference script prints one [PROGRESS i/N] line per item for visibility.
7. **Inference script**: Qwen/Qwen3.6-27B NF4 across dual T4 (13.5GiB/GPU),
   MAX_NEW_TOKENS=384, thinking off, resume-safe JSONL, MAX_ITEMS_PER_RUN cap,
   truncation guard (unparseable/capped outputs never enter fine-tune set).

Measured throughput: ~65 s/item steady state; ~19 min startup/session;
completion tokens avg ~81 / max ~92.

---

## 4. Work Completed (chronological; full detail in CHANGES.md)

### Session A - Platform repair (dispatch was 100% broken)
- ROOT CAUSE: kaggle 2.x reads tokens ONLY from KAGGLE_API_TOKEN env or
  ~/.kaggle/access_token - never $KAGGLE_CONFIG_DIR/access_token. Fixed by
  exporting per-account token in get_account_env().
- Notebook normalization: kernelspec injection + raw-code wrapping.
- Slug fix: metadata id must equal slugified title exactly (random suffixes
  caused permanent 409s on every re-push).
- Quota CSV schema update; output-download flag fix; enable_gpu for l4/a100/h100.
- DB wipe + clean re-registration (real usernames: darkzone16, jayadithyx16).
- Verified: single run COMPLETE on 2×T4; distributed 2×50 items COMPLETE with
  zero overlap; artifact pull verified; stop endpoint works.

### Session B - UI/UX overhaul
- Back-button login loop fixed: /login redirects authed users, no-store cache
  headers, History-API tab navigation (/?tab=<id>), deep links, popstate.
- Geist Pixel = default font everywhere; Pixelarticons (MIT) bundled locally
  replace Lucide CDN; flat theme - ALL blue gradients/glows removed.
- Run Catalog Logs button fixed (now switches to Terminal tab); logs load via
  plain HTTP first (WS only for live tail); action buttons flex+gap aligned.

### Session C - Secrets + streaming reliability
- HF_TOKEN injection: .env -> kernel preamble via lazy get_kernel_env_defaults()
  (no restart needed); read scope suffices; verified on real kernel.
- Log streamer hardened: stderr drained (undrained pipe = silent deadlock),
  reconnect with backoff until terminal state, ensure_log_stream() self-heal on
  Logs open, skip_initial WS flag prevents duplicate dumps.

### Session D - Multi-session distribution + the ongoing failure
- `sessions_per_account` (default 2, clamp 1-2) on JSON + form APIs.
- Per-account availability: active DB runs filtered to GPU accelerators, CLI-
  refreshed; **reap-grace window**: recently stopped/error GPU rows (≤20 min,
  end_time falls back to start_time).
- Session-cap retries in push_kernel: dedicated delays [30,60,90,150,240,300]
  (~15.5 min coverage); scans BOTH stdout and stderr for retryable errors.
- `reclaim_slots()`: actively finds dashboard-managed kernels still RUNNING or
  QUEUED (within 30-min lookback) and pushes cancel stubs to free slots fast;
  wired into distribute_and_launch pre-launch; foreign kernels only warned,
  never auto-cancelled.

### Session E - Current iteration (kernel_buffering + dedupe + retry hardening)
- stdout/stderr scan widened for push retry detection (combined_out = out_str +
  "\\n" + err_str) so 409 Conflict errors on STDout are caught.
- `_busy_gpu_sessions` FIXED: counts DISTINCT `kernel_ref` values instead of
  DB row count (was inflating busy count when duplicate rows existed per
  kernel_ref — failed attempt + successful relaunch share a ref).
- Launched 4-shard distributed test with `sessions_per_account=2`; shard 0
  succeeded (running), shards 1-3 errored with `kernelworkerstatus.cancel_acknowledged`
  due to Kaggle session-cap enforcement during rapid sequential pushes.
- Fixed stagger between same-account pushes: **4s → 30s** (Kaggle needs >10s
  to settle session cap between kernel pushes).
- Manual reclaim test validated: the reclaim_slots() feature works correctly
  (found and cancelled RUNNING/QUEUED holders), but a manual invocation mid-
  flight destroyed a live test job — lesson applied: reclaim runs only inside
  launch path.

---

## 5. Current Failure — RESOLVED (was: Kaggle session cap vs rapid pushes)

**Resolution (launch #5, ~15:28 UTC):** the combination below produced the
first clean 4-shard dispatch — zero CANCEL_ACKNOWLEDGED errors. Full results
in CHANGES.md §45.

**Root cause (historical):** Kaggle enforces its 2-session batch-GPU cap during
kernel push. Pushing 2 kernels from the same account in rapid succession (<~10s
apart) causes the second push to see the first session still occupying the cap.

**Fixes built so far:**
- Stagger between same-account pushes: **4s → 30s** (giving Kaggle ample time
  to finalize each session's cap before the next push).
- Reap-grace window (20 min) in `_busy_gpu_sessions`: recently stopped/error
  GPU rows count as busy; end_time falls back to start_time for legacy rows;
  distinct kernel_ref counting prevents inflating busy count from duplicate DB rows.
- Session-cap retries in push_kernel (~15.5 min total coverage), scanning both
  output streams.
- reclaim_slots(): actively cancels lingering RUNNING/QUEUED holders; wired into
  launch path only (never fired manually at runtime again).

**Duplicate-row inflation (now fixed):** `_busy_gpu_sessions` previously counted
all DB rows per account. When a kernel failed once then relaunched, two DB rows
shared the same kernel_ref → busy count was double the real sessions → availability
always showed 0 free slots. **Fix:** collect DISTINCT kernel_ref values per
account, return `{a: len(distinct_refs)}`. Verified: both accounts now show
accurate counts (e.g., 0/0, 1/1, 2/2) instead of inflated 2/2, 4/4, 6/6.

---

## 6. Launch History

| Launch # | Time | Result |
|---|---|---|
| 1 (Session D) | ~13:26 | Partial: shard 0 COMPLETE on darkzone16; shards 1-3 errored (cancel_acknowledged). All runners eventually cancelled manually. |
| 2 (Session D retry) | ~13:32 | Manually fired `reclaim_slots()` → killed 3 healthy runners mid-flight (own goal). |
| 3 (Session E, first attempt) | ~14:12 | 4 shards launched; shard 0 **running** on darkzone16; shards 1-3 errored (cancel_acknowledged). |
| 4 (Session E, relaunch prep) | ~15:05 | Cancelled before push; grace window still active. Superseded by #5. |
| 5 (**SUCCESS**) | ~15:28-17:00 | **All 4 shards COMPLETE.** 200/200 items, 200 unique ids, fail=0, wall time ~92 min. Details: CHANGES.md §45. |

## 7. Recovery Plan — EXECUTED & CLOSED

All steps completed 15:51-17:10 UTC:
1. ✅ Grace window cleared; slots verified free.
2. ✅ Server restarted (~15:51; process had died) — launch #5 had already fired
   at 15:28 with the fixed 30s stagger.
3. ✅ All 4 shards confirmed RUNNING via live `kernels status`.
4. ✅ All 4 reached their `[LIMIT]` cap + "Shard X Complete! Labeled 50 items".
5. ✅ Artifacts pulled; aggregate check: 200 lines / 200 unique ids / 0 bad.
6. ✅ CHANGES.md §45 written with results + verification.

Timings: shard durations 75.0 / 86.3 / 86.7 / 91.6 min; effective aggregate
~27.6 s/item across 4 sessions (vs ~65-90 s/item single-session); tokens
avg 89 / max 102.

### Next options (pick one)
- Scale horizontally: add Kaggle accounts (only linear throughput lever).
- Micro-batching inside each session (~2-4x, engine rewrite).
- Rotate tokens listed in §8 watchlist.

## 8. Watchlist / Known Issues

- Log followers can die mid-run while the kernel keeps running; remote log API
  may lag minutes behind a RUNNING kernel. Empty output ≠ stalled job — verify
  via `kernels status`, and use Logs `?fetch_remote=true` to backfill.
- mistune/nbconvert SyntaxWarnings and bitsandbytes alignment warnings in kernel
  logs are harmless noise.
- 2x Kaggle tokens, APP_AUTH_TOKEN, Telegram bot token, HF token: rotate when
  convenient — none are committed to git.

---

## 9. Output-Recovery Arc (2026-08-23 → ongoing)

### What we are trying to solve

When a notebook is **stopped** (or fails), the user's real partial
artifacts (e.g. `task_a_labeled_shard_*.jsonl` with thousands of
labelled rows) must survive the stop and must still be retrievable
later — even after the user has deleted local copies. Today the
dashboard's "Pull Output Files from Kaggle" button on a stopped
notebook returns only the log file, which makes the user (rightly)
furious.

### What we learned by reading the installed kaggle 2.2.4 / kagglesdk

1. **Kaggle finalizes every version** — complete, error, **and
   cancelled** — with its own output snapshot. The user's
   intuition ("I can see the partial outputs on the website") is
   correct.
2. `kaggle kernels output` downloads **only the latest version**.
3. Our `stop_kernel` pushes an exit-stub that **becomes** the
   latest version, so latest-only pulls return the stub's log
   while the real partial data sits one version back.
4. The CLI parses `<owner>/<slug>/<version>` for the kernel arg
   but **silently drops the version** on output download. The
   underlying SDK request
   `ApiListKernelSessionOutputRequest.version_label` **is** honored
   by the backend (per its serializer metadata).

### What we shipped, in order

- **`app/services/kaggle_versioned_output.py`** (v1, then v2): a
  helper invoked as a subprocess under the account's env that
  downloads **a specific kernel version's** output. v1 imported
  `kaggle.api.kaggle_api_extended` and crashed at import time on
  the server (the `kaggle` Python package wasn't installed even
  though the CLI was). v2 is **pure httpx** — replicates the
  kagglesdk wire format exactly (`POST /api/v1/kernels/{list,output}`,
  camelCase JSON, `Authorization: Bearer <access_token>` from
  `KAGGLE_API_TOKEN` or `<KAGGLE_CONFIG_DIR>/access_token` or
  `~/.kaggle/access_token`). Tries every plausible `versionLabel`
  spelling (`"7"`, `"version-7"`, `"version7"`), paginates with
  `nextPageToken`, and **rejects log-only results** (the stop-stub
  signature) — only saves real artifacts. Private-kernel lookup
  fallback: if `kernels/list` `search=<slug>` misses, page the
  user's full kernel list and match by `ref`/`slug`.
- **`runs.output_version INTEGER`** column with a lightweight
  `_ensure_column` ALTER migration in `init_db` so existing
  databases gain the column on next startup without losing data.
  `set_run_output_version(run_id, version)` setter.
- **`KaggleService.download_latest_outputs(account, ref, run_id,
  prefer_version=None, probe_previous_for_stop=False)`** — every
  pull path (manual Pull, single-file auto-pull, ZIP download,
  merge-cart auto-pull) routes through this. Returns `(path,
  version_used)`. `prefer_version` short-circuits the lookup;
  `probe_previous_for_stop` triggers a `current-1` rescue for
  legacy unpinned stopped runs.
- **`stop_kernel` pins `output_version`** for the run and for any
  siblings sharing the kernel_ref, **before** pushing the exit
  stub, so every later pull can skip the stub.
- **Monitor auto-sync** uses the same version-aware path and
  persists `used_version` on first discovery.
- **Status normalization** (`_normalize_kernel_status`) folds
  enum-style CLI output (`kernelworkerstatus.cancel_acknowledged`)
  into our five statuses. One-time DB repair:
  `UPDATE runs SET status='stopped' WHERE lower(status) LIKE '%cancel%'`.
- **Helper diagnostics**: when nothing is recovered, the helper
  exits 3 and `download_outputs_of_version` logs the per-label
  `notes` (which label spellings were tried, which gave log-only
  pages, which failed with HTTP errors) into the service log.
  Before this, failures were invisible to the user.

### Current failure (still open)

Live debugging against `https://kabila-aws.isroot.in`:

- `POST /api/runs/run_20260826_025953_5ecbf3/files/pull` →
  `{"success":true,"message":"Downloaded 1 file(s) from Kaggle.","files":[{"name":"…shard-1.log","size":913,…}]}`. Still log-only.
- 7 sibling shards (shard-1..8) on `@ganeshmohana`,
  `@nagavignesh1729`, `@rubeshmanogar`, `@vsamarnath` are all
  `stopped` with `output_version=None`.
- The pull message format ("Downloaded N file(s) from Kaggle.")
  confirms the new router code is live on the server. Whether
  the server is running the *latest* helper (`45ca563` family) is
  not yet independently verified — it requires the user to
  restart uvicorn.

The open question that determines whether the fix can succeed
without a Kaggle-side workaround: **does the Kaggle backend
actually honor `versionLabel`?** The SDK serializer says it
does. If the backend silently ignores it, our helper will see
the latest version (the stub) regardless of the label, reject it
as log-only, fall back to `current_version - 1` for the legacy
rescue, and that label also goes to the same latest-stub
backend. Worst case: we get log-only as before, but we never
falsely claim success.

### Test suite (39 tests, all green)

- `test_distributor.py` — multi-session sharding, silent
  reduction, conflict rejection, stop-workload endpoint.
- `test_automation.py` — DB and run CRUD.
- `test_inference_script.py` — inference script harness: install
  flags, model class, single-shard end-to-end, resume skips
  processed, multi-shard partition, env-var int coercion, JSON
  extract cases, no-GPU exit, truncated-generation skip.
- `test_merge_cat.py` — `merge_selected_files` raw `cat`
  semantics: exact bytes + order, duplicate ids preserved, binary
  integrity, auto-pull on missing → 502, empty cart → 400.
- `test_quota_refresh.py` — semaphore cap, single-flight join,
  per-call timeout.
- `test_stop_capture.py` — `stop_kernel` recovers cancelled
  version + pins `output_version`; no-version path still stops
  cleanly.
- `test_stopped_pulls.py` — pinned wins over latest, legacy
  unpinned stop rescued via `-1` probe, completed run uses
  current version.

### Next concrete move (for the operator)

```bash
# on kabila-aws host, inside this repo
git pull        # or however the repo is synced
# restart the tmux uvicorn pane
# then re-run the same pull:
#   POST /api/runs/run_20260826_025953_5ecbf3/files/pull
```

If the response is still one log file, the new server log will
show the helper's per-label `notes` (which label spellings gave
log-only pages vs HTTP errors vs successful file downloads) —
that single piece of information is enough to know whether
Kaggle's backend is honoring `versionLabel` or ignoring it,
and therefore whether the next move is server-side or
Kaggle-side.
