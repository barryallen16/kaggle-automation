---
name: port-to-kaggle
description: Port a .py or .ipynb script to run in kaggle-automation (single + distributed sharding).
---

# Port to Kaggle-Automation

Takes a Python script or notebook and rewrites it so the dashboard can push it to Kaggle as-is.

## Run it

```bash
python skills/port-to-kaggle/scripts/port_to_kaggle.py in.py --out out.py --title "My Job" --accelerator nvidia-tesla-t4-x2
python skills/port-to-kaggle/scripts/port_to_kaggle.py in.ipynb --out out.ipynb --check   # report only
python skills/port-to-kaggle/scripts/port_to_kaggle.py --version
```

Same extension in → out. Safe to re-run (idempotent, tagged cells / marker checks).

The JSON report ends with `next`: ready-to-run commands for the next step (re-check, push, sharded push).
Errors print to stdout as `{"error": ..., "help": ...}` with exit code 2, so there is no traceback to parse.

## What it fixes (auto)

1. Notebook normalized via `KaggleService.ensure_executable_notebook` (kernelspec, raw-py wrap).
2. Shard-fallback header (`SHARD_ID`, `TOTAL_SHARDS`, `START_INDEX`, `END_INDEX`, env-coerced to int) — standalone-safe, overridden by the distributor's real header at push time.
3. Line-buffered stdout/stderr.
4. `WORKING_DIR` (`/kaggle/working` else cwd) + `SCRATCH_DIR` (creates `/kaggle/tmp` on Kaggle, else working dir — never bare cwd, so large downloads don't land in published outputs).

## What it warns about (you fix)

- No GPU guard, title >50 chars, P100 accelerator, hardcoded `HF_TOKEN`/API keys, datasets into `/kaggle/working` (use scratch), unsharded `OUTPUT_FILE`, bare `pip install` (use `uv pip install --system`).

## Not handled here (server does it at push)

Secrets preamble (`HF_TOKEN`, `WANDB_*`), `MAX_RUNTIME_MINUTES` quota cap, title auto-rename (`Title (2)` when taken) + shard-title fit into 50 chars, slug conflict check, output-version pinning. See `app/services/kaggle_service.py`, `app/routers/runs.py`, `app/services/workload_distributor.py`.

## Launch it

The `next` commands in the report push the ported file straight to the server:

```bash
# single run
curl -sS -X POST http://127.0.0.1:8000/api/runs/upload-and-launch \
  -F file=@out.py -F account_username=<account> \
  -F title="My Job" -F accelerator=nvidia-tesla-t4-x2

# sharded across accounts
curl -sS -X POST http://127.0.0.1:8000/api/distributed/upload-and-launch \
  -F file=@out.py -F base_title="My Job" -F accounts=<account1,account2> \
  -F total_items=10000000 -F accelerator=nvidia-tesla-t4-x2
```

Add `-H 'Cookie: kaggle_auto_session=<value>'` to both calls when the server runs with `APP_AUTH_TOKEN` set.

## Reference port

`kaggle_batch_inference_task_a.py` — shard fallback, scratch/outputs split, `flush=True`, resume skip-set, `uv --system` installs.
