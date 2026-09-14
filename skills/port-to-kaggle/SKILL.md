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
```

Same extension in → out. Safe to re-run (idempotent, tagged cells / marker checks).

## What it fixes (auto)

1. Notebook normalized via `KaggleService.ensure_executable_notebook` (kernelspec, raw-py wrap).
2. Shard-fallback header (`SHARD_ID`, `TOTAL_SHARDS`, `START_INDEX`, `END_INDEX`, env-coerced to int) — standalone-safe, overridden by the distributor's real header at push time.
3. Line-buffered stdout/stderr.
4. `WORKING_DIR` (`/kaggle/working` else cwd) + `SCRATCH_DIR` (`/kaggle/tmp` else cwd).

## What it warns about (you fix)

- No GPU guard, title >50 chars, P100 accelerator, hardcoded `HF_TOKEN`/API keys, datasets into `/kaggle/working` (use scratch), unsharded `OUTPUT_FILE`, bare `pip install` (use `uv pip install --system`).

## Not handled here (server does it at push)

Secrets preamble, `MAX_RUNTIME_MINUTES` quota cap, slug conflict check, output-version pinning. See `app/services/kaggle_service.py`, `app/routers/runs.py`, `app/services/workload_distributor.py`.

## Reference port

`kaggle_batch_inference_task_a.py` — shard fallback, scratch/outputs split, `flush=True`, resume skip-set, `uv --system` installs.
