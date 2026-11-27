---
name: kaggle-runner
description: Manage and launch Kaggle batch runs on the remote automation server at kabila-aws.isroot.in (or localhost). Use this skill whenever launching, running, monitoring, stopping, fetching logs, or pulling/downloading outputs for Kaggle jobs, kernels, or distributed workloads; or when configuring the remote app_auth token in .env for persistent session access.
---

# Kaggle runner

CLI client for `https://kabila-aws.isroot.in` (overridable via `KABILA_API_URL`) that writes compact TOON output and structured errors to stdout.

## Quick status check

Run the script with no arguments to print the server connection status, account list with free GPU slots, active runs, and common commands:

```bash
python skills/kaggle-runner/scripts/kaggle_runner.py
```

Example output:

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
help[2]:
  Run `kaggle_runner run <file> --title <title>` to launch a kernel
  Run `kaggle_runner runs --active` to inspect active runs
```

## Authentication and secret storage

When `APP_AUTH_TOKEN` is enabled on the server, save the token to `.env` with `auth`.
The command checks `.gitignore` first and appends `.env` if missing so secrets stay untracked:

```bash
python skills/kaggle-runner/scripts/kaggle_runner.py auth --token "<YOUR_APP_AUTH_TOKEN>"
```

Saved keys in `.env`:
- `KABILA_API_URL`: server URL (defaults to `https://kabila-aws.isroot.in`).
- `KABILA_APP_AUTH_TOKEN`: bearer secret matching `APP_AUTH_TOKEN`.

Re-running `auth` with the same token returns a no-op with exit code `0`.

## Version check

```bash
python skills/kaggle-runner/scripts/kaggle_runner.py --version
```

Flags `-v`, `-V`, and `--version` exit `0` before loading network or `.env` state.

## Preparing scripts before launch

Run `port_to_kaggle.py` first so stdout is line-buffered, scratch writes go to `/kaggle/tmp`, and shard fallback variables exist:

```bash
python skills/port-to-kaggle/scripts/port_to_kaggle.py in.py --out out.py --title "Job" --accelerator nvidia-tesla-t4-x2
```

Inside Kaggle kernels, install packages with `uv pip install --system`.

## Commands

### Status and accounts

List free GPU capacity across accounts:

```bash
python skills/kaggle-runner/scripts/kaggle_runner.py status
```

List accounts and remaining weekly quota hours:

```bash
python skills/kaggle-runner/scripts/kaggle_runner.py accounts
```

### Single run

Launch a single kernel on the first free account (or pin one with `--account`):

```bash
python skills/kaggle-runner/scripts/kaggle_runner.py run out.py \
  --title "Inference Job" \
  --accelerator nvidia-tesla-t4-x2
```

Options:
- `--account <username>`: target a specific Kaggle account.
- `--accelerator`: hardware shape (default `nvidia-tesla-t4-x2`; free-tier accounts support `none`, `nvidia-tesla-t4-x2` (`GPU T4 x2`), and `tpu-v5e-8` (`TPU v5e-8`); eligible accounts/competitions accept `nvidia-tesla-t4-highmem`, `nvidia-l4`, `nvidia-a100`, `nvidia-h100`, `tpu-v6e-8`, or another valid slug). `--gpu` is an alias.
- `--trial`: set timeout to 300 seconds.
- `--no-internet`: turn off internet access inside the kernel.
- `--timeout <seconds>`: custom timeout in seconds.

### Distributed sweep

Split items across multiple accounts:

```bash
python skills/kaggle-runner/scripts/kaggle_runner.py distributed out.py \
  --title "Batch Sweep" \
  --accounts "acc1,acc2" \
  --total-items 100000 \
  --accelerator nvidia-tesla-t4-x2
```

The server splits item ranges across the accounts and starts the shards.

### Inspect runs

List active runs:

```bash
python skills/kaggle-runner/scripts/kaggle_runner.py runs --active
```

List recent runs with custom columns:

```bash
python skills/kaggle-runner/scripts/kaggle_runner.py runs --limit 15 --fields id,title,status,account
```

### Fetch logs

Read the last 100 cached log lines:

```bash
python skills/kaggle-runner/scripts/kaggle_runner.py logs <RUN_ID> --tail 100
```

Read the full cached log:

```bash
python skills/kaggle-runner/scripts/kaggle_runner.py logs <RUN_ID> --full
```

Fetch fresh logs from Kaggle:

```bash
python skills/kaggle-runner/scripts/kaggle_runner.py logs <RUN_ID> --remote
```

### List, pull, and download outputs

List remote and server-cached output files:

```bash
python skills/kaggle-runner/scripts/kaggle_runner.py outputs <RUN_ID>
```

Pull outputs from Kaggle onto the server:

```bash
python skills/kaggle-runner/scripts/kaggle_runner.py outputs <RUN_ID> --pull
```

Download one output file or a zip archive of all outputs to local disk:

```bash
python skills/kaggle-runner/scripts/kaggle_runner.py outputs <RUN_ID> --download results.jsonl --out ./results.jsonl
python skills/kaggle-runner/scripts/kaggle_runner.py outputs <RUN_ID> --zip --out ./run_outputs.zip
```

### Stop runs

Stop one run:

```bash
python skills/kaggle-runner/scripts/kaggle_runner.py stop <RUN_ID>
```

Stop all active shards in a workload:

```bash
python skills/kaggle-runner/scripts/kaggle_runner.py stop <WORKLOAD_ID> --workload
```

## Exit codes

Errors print structured objects to stdout with next-step hints and exit with standard codes:
- `0`: success or no-op.
- `1`: blocked operation or server error.
- `2`: invalid flag or missing input file (lists valid flags on stdout).
