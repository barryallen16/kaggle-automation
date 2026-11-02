---
name: kaggle-runner
description: Manage and launch Kaggle batch runs on the remote automation server at kabila-aws.isroot.in (or localhost). Use this skill whenever launching, running, monitoring, stopping, or fetching logs for Kaggle jobs, kernels, or distributed workloads; or when configuring the remote app_auth token in .env for persistent session access.
---

# Kaggle Runner

Automate Kaggle batch jobs and parameter sweeps through `https://kabila-aws.isroot.in`.
The tool implements Agent eXperience Interface (AXI) standards with TOON output, structured errors, and zero-call ambient discovery.

## Ambient Discovery (Content First)

Running the CLI with no arguments prints the live system state immediately.
You do not need to read documentation or run multiple diagnostic calls to orient yourself.

```bash
python skills/kaggle-runner/scripts/kaggle_runner.py
```

The output returns the executable path, connection status, account list with free GPU slots, active runs, and next-step commands:

```
bin: ~/skills/kaggle-runner/scripts/kaggle_runner.py
description: Kaggle batch automation runner and monitor at kabila-aws.isroot.in
url: https://kabila-aws.isroot.in
status: ok
total_free_gpu_slots: 3
accounts[2]{username,free_slots,busy_sessions,quota_hours}:
  acc1,2,0,30.0
  acc2,1,1,14.5
active_runs_count: 1
help[3]:
  Run `kaggle_runner run <file> --title <title>` to launch a kernel
  Run `kaggle_runner runs --active` to inspect active runs
```

## Authentication and Secret Persistence

The server authenticates API calls via a shared secret (`APP_AUTH_TOKEN`).
When given an `app_auth` key, persist it into `.env` so subsequent sessions work without re-prompting.

### Persistence Rules

Always verify that `.env` is listed in `.gitignore` before writing any secrets.
Never commit `.env` or credentials to git.
The CLI automates this check and appends `.env` to `.gitignore` when missing.

```bash
python skills/kaggle-runner/scripts/kaggle_runner.py auth --token "<YOUR_APP_AUTH_TOKEN>"
```

Configuration variables stored in `.env`:
- `KABILA_API_URL`: Server endpoint (defaults to `https://kabila-aws.isroot.in`).
- `KABILA_APP_AUTH_TOKEN`: Shared secret credential.

The command is idempotent.
Re-running with an already configured token reports a no-op with exit code 0.

## Fast Version Probe

The tool responds immediately to version probes before loading heavy command graphs:

```bash
python skills/kaggle-runner/scripts/kaggle_runner.py --version
```

Bare flags `-v`, `-V`, and `--version` exit 0 instantly.

## Pre-flight Script Preparation

Run the `port-to-kaggle` tool on Python scripts or notebooks before launching.
It ensures unbuffered stdout, configures scratch paths in `/kaggle/tmp`, and injects shard variable fallbacks.

```bash
python skills/port-to-kaggle/scripts/port_to_kaggle.py in.py --out out.py --title "Job" --accelerator nvidia-tesla-t4-x2
```

Ensure scripts install dependencies using `uv pip install --system`.

## Command Reference

### 1. Status and Accounts

Check free GPU capacity across all registered Kaggle accounts:

```bash
python skills/kaggle-runner/scripts/kaggle_runner.py status
```

List registered accounts and remaining weekly quota hours:

```bash
python skills/kaggle-runner/scripts/kaggle_runner.py accounts
```

### 2. Single Run Launch

Launch a single kernel run on the first available Kaggle account:

```bash
python skills/kaggle-runner/scripts/kaggle_runner.py run out.py \
  --title "Inference Job" \
  --accelerator nvidia-tesla-t4-x2
```

Valid options:
- `--account <username>`: Pin execution to a specific account instead of auto-selecting.
- `--accelerator`: `nvidia-tesla-t4-x2` (dual T4, default), `nvidia-tesla-t4` (single T4), `v3-8` (TPU), or `none` (CPU). `--gpu` is accepted as an alias.
- `--trial`: Run with a 5-minute pre-flight timeout.
- `--no-internet`: Disable internet access inside the kernel container.
- `--timeout <seconds>`: Custom timeout in seconds.

### 3. Distributed Workload Launch

Distribute a sweep or dataset across multiple Kaggle accounts:

```bash
python skills/kaggle-runner/scripts/kaggle_runner.py distributed out.py \
  --title "Batch Sweep" \
  --accounts "acc1,acc2" \
  --total-items 100000 \
  --accelerator nvidia-tesla-t4-x2
```

The server divides items across accounts and starts parallel shards automatically.

### 4. Inspect Runs

List in-flight runs using minimal default schemas:

```bash
python skills/kaggle-runner/scripts/kaggle_runner.py runs --active
```

List recent runs with custom fields:

```bash
python skills/kaggle-runner/scripts/kaggle_runner.py runs --limit 15 --fields id,title,status,account
```

Definitive empty states are reported explicitly when no runs are active.

### 5. Fetch Logs

View recent log lines with automatic truncation:

```bash
python skills/kaggle-runner/scripts/kaggle_runner.py logs <RUN_ID> --tail 100
```

Fetch complete untruncated logs:

```bash
python skills/kaggle-runner/scripts/kaggle_runner.py logs <RUN_ID> --full
```

Fetch logs directly from Kaggle API instead of local cache:

```bash
python skills/kaggle-runner/scripts/kaggle_runner.py logs <RUN_ID> --remote
```

### 6. Stop Runs

Terminate a single run:

```bash
python skills/kaggle-runner/scripts/kaggle_runner.py stop <RUN_ID>
```

Terminate all active shards of a distributed workload:

```bash
python skills/kaggle-runner/scripts/kaggle_runner.py stop <WORKLOAD_ID> --workload
```

## Error Handling Contract

Errors print to stdout in structured form with actionable hints and standard exit codes:
- `0`: Success or idempotent no-op.
- `1`: Operation blocked or server error.
- `2`: Usage error (unrecognized flag, missing file).

Unrecognized flags fail loud with valid options listed on stdout, allowing single-turn self-correction.
