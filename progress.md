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
