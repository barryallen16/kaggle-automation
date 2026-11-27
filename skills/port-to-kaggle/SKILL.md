---
name: port-to-kaggle
description: Port a .py or .ipynb script to run in kaggle-automation (single + distributed sharding).
---

# Port to kaggle-automation

Rewrites a `.py` script or `.ipynb` notebook so the server can push it to Kaggle as a single or distributed job.

## Usage

```bash
python skills/port-to-kaggle/scripts/port_to_kaggle.py in.py --out out.py --title "My Job" --accelerator nvidia-tesla-t4-x2
python skills/port-to-kaggle/scripts/port_to_kaggle.py in.ipynb --out out.ipynb --check
python skills/port-to-kaggle/scripts/port_to_kaggle.py --version
```

Keep the input and output extensions the same.
Re-running on an already ported file is idempotent using tagged cells and marker checks.
Pass `--check` to inspect issues without writing the output file.

The JSON report ends with a `next` field containing copy-pasteable commands for checking, single launch, or distributed launch.
Usage errors print `{"error": ..., "help": ...}` to stdout with exit code `2` instead of a traceback.

## Automatic edits

The tool normalizes notebooks via `KaggleService.ensure_executable_notebook` to add Python 3 kernelspec metadata and wrap raw Python into cells.
It prepends a standalone shard fallback header (`SHARD_ID`, `TOTAL_SHARDS`, `START_INDEX`, `END_INDEX`, coerced to `int`) that the distributor overrides at push time.
It also turns on line-buffered `stdout` and `stderr`, sets `WORKING_DIR` (`/kaggle/working` or current directory), and sets `SCRATCH_DIR` (`/kaggle/tmp` on Kaggle or `WORKING_DIR` locally) so large downloads do not bloat published outputs.

## Warnings

The linter warns on missing GPU availability guards, titles longer than 50 characters, P100 accelerators, hardcoded `HF_TOKEN` or API keys, input downloads into `/kaggle/working`, `OUTPUT_FILE` paths that ignore `SHARD_ID`, and `pip install` calls without `uv pip install --system`.

## Server-side behavior

The server injects `HF_TOKEN`, `WANDB_*`, and `MAX_RUNTIME_MINUTES` at push time in `app/services/kaggle_service.py`, `app/routers/runs.py`, and `app/services/workload_distributor.py`.
It also handles 50-character title trimming for shards, duplicate-title renaming (`Title (2)`), slug conflict checks, and output version tracking.

## Launch

Run via `kaggle_runner.py` (which loads `.env` automatically), or post directly with `curl`:

```bash
python skills/kaggle-runner/scripts/kaggle_runner.py run out.py \
  --title "My Job" --accelerator nvidia-tesla-t4-x2

curl -sS -X POST http://127.0.0.1:8000/api/runs/upload-and-launch \
  -H "Authorization: Bearer $KABILA_APP_AUTH_TOKEN" \
  -F file=@out.py -F account_username=<account> \
  -F title="My Job" -F accelerator=nvidia-tesla-t4-x2

curl -sS -X POST http://127.0.0.1:8000/api/distributed/upload-and-launch \
  -H "Authorization: Bearer $KABILA_APP_AUTH_TOKEN" \
  -F file=@out.py -F base_title="My Job" -F accounts=<account1,account2> \
  -F total_items=10000000 -F accelerator=nvidia-tesla-t4-x2
```

Pass `Authorization: Bearer <APP_AUTH_TOKEN>` whenever `APP_AUTH_TOKEN` is set on the server.

## Reference script

`kaggle_batch_inference_task_a.py` shows shard fallbacks, scratch versus output directories, `flush=True`, resume skip-sets, and `uv pip install --system`.
