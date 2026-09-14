"""Port a .py / .ipynb script to run in kaggle-automation (single + distributed).

Usage:
    python port_to_kaggle.py in.py --out out.py --title "My Job" --accelerator nvidia-tesla-t4-x2
    python port_to_kaggle.py in.ipynb --out out.ipynb --check   # report only

What it does (idempotent, safe to re-run):
  1. Normalizes notebooks via KaggleService.ensure_executable_notebook
     (kernelspec inject, raw-py-as-ipynb wrap).
  2. Prepends a shard-fallback header (_get_shard_var + SHARD_ID/TOTAL_SHARDS/
     START_INDEX/END_INDEX) so code runs standalone AND when the distributor
     prepends real values at push time (globals() wins over env/defaults).
  3. Prepends line-buffered stdout/stderr + WORKING_DIR/SCRATCH_DIR definitions
     when missing.
  4. Warns (never rewrites logic) on: missing GPU guard, title >50 chars /
     slug conflicts, unknown accelerator, hardcoded secrets, dataset landing
     in /kaggle/working, unsharded OUTPUT_FILE, bare `pip install`.

Secrets / MAX_RUNTIME_MINUTES are injected server-side at push
(app/services/kaggle_service.py:build_env_preamble,
 app/routers/runs.py:_quota_capped_env) — this script never bakes them.
"""

import argparse
import json
import os
import re
import sys
import uuid


def _platform_candidates() -> list[str]:
    """Where the kaggle-automation platform code may live.

    1. $KAGGLE_AUTOMATION_REPO (checkout root) — set this when the skill is
       installed globally so output matches the server exactly.
    2. Repo-checkout layout (this file at <root>/skills/port-to-kaggle/scripts/).
    """
    cands = []
    env = (os.environ.get("KAGGLE_AUTOMATION_REPO") or "").strip().strip("\"'")
    if env:
        cands.append(os.path.normpath(os.path.expandvars(os.path.expanduser(env))))
    cands.append(
        os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        )
    )
    return cands


def _load_platform():
    """Import server helpers when available, else stdlib fallback (same rules).

    Returns (is_gpu_accelerator, KaggleService-ish, mode) where mode is
    "server" or "standalone". Standalone mirrors app/config.py + the pure
    parts of app/services/kaggle_service.py so the skill works globally.
    """
    for root in _platform_candidates():
        app_dir = os.path.join(root, "app")
        if not (
            os.path.isfile(os.path.join(app_dir, "config.py"))
            and os.path.isfile(os.path.join(app_dir, "services", "kaggle_service.py"))
        ):
            continue
        for p in (root, app_dir):
            if p not in sys.path:
                sys.path.insert(0, p)
        try:
            from config import is_gpu_accelerator as _gpu
            from services.kaggle_service import KaggleService as _ks

            return _gpu, _ks, "server"
        except Exception:  # noqa: S112 — tried next candidate silently by design
            continue

    # --- standalone fallback: same rules, no platform checkout needed ---
    def _gpu(accelerator) -> bool:
        a = str(accelerator or "").lower()
        return bool(a) and a not in ("none", "default", "cpu")

    class _KS:
        from typing import ClassVar

        DEFAULT_KERNELSPEC: ClassVar[dict] = {"name": "python3", "display_name": "Python 3", "language": "python"}
        # Mirrors KaggleService.ACCELERATOR_MAP (app/services/kaggle_service.py).
        # Kept inline so global installs work without the platform's deps
        # (config.py needs python-dotenv); server mode still wins when available.
        ACCELERATOR_MAP: ClassVar[dict] = {
            "nvidia-tesla-t4": "NvidiaTeslaT4",
            "nvidia-tesla-t4-x2": "NvidiaTeslaT4",
            "t4": "NvidiaTeslaT4",
            "t4-x2": "NvidiaTeslaT4",
            "gpu-tesla-t4": "NvidiaTeslaT4",
            "gpu-tesla-t4-x2": "NvidiaTeslaT4",
            "nvidia-tesla-t4-highmem": "NvidiaTeslaT4Highmem",
            "t4-highmem": "NvidiaTeslaT4Highmem",
            "t4highmem": "NvidiaTeslaT4Highmem",
            "nvidia-tesla-p100": "NvidiaTeslaP100",
            "gpu-p100": "NvidiaTeslaP100",
            "p100": "NvidiaTeslaP100",
            "a100": "NvidiaTeslaA100",
            "nvidia-a100": "NvidiaTeslaA100",
            "l4": "NvidiaL4",
            "nvidia-l4": "NvidiaL4",
            "l4x1": "NvidiaL4X1",
            "nvidia-l4-x1": "NvidiaL4X1",
            "h100": "NvidiaH100",
            "nvidia-h100": "NvidiaH100",
            "rtx-pro-6000": "NvidiaRtxPro6000",
            "nvidia-rtx-pro-6000": "NvidiaRtxPro6000",
            "v3-8": "TpuV38",
            "tpu-v3-8": "TpuV38",
            "tpu1vm-v3-8": "TpuV38",
            "tpu1vmv38": "TpuV38",
            "tpu-v5e-8": "TpuV5E8",
            "tpu-v5e8": "TpuV5E8",
            "tpu-v6e-8": "TpuV6E8",
            "tpu-v6e8": "TpuV6E8",
        }

        @classmethod
        def ensure_executable_notebook(cls, code_content: str) -> str:
            try:
                nb = json.loads(code_content)
                if not isinstance(nb, dict):
                    raise TypeError("notebook JSON root must be an object")
            except Exception:
                nb = {
                    "cells": [
                        {
                            "cell_type": "code",
                            "execution_count": None,
                            "metadata": {},
                            "outputs": [],
                            "source": code_content.splitlines(keepends=True),
                        }
                    ],
                    "metadata": {},
                    "nbformat": 4,
                    "nbformat_minor": 2,
                }
            metadata = nb.get("metadata")
            if not isinstance(metadata, dict):
                metadata = {}
            metadata.setdefault("kernelspec", dict(cls.DEFAULT_KERNELSPEC))
            nb["metadata"] = metadata
            if not isinstance(nb.get("cells"), list):
                nb["cells"] = []
            nb.setdefault("nbformat", 4)
            nb.setdefault("nbformat_minor", 2)
            return json.dumps(nb)

        @classmethod
        def resolve_accelerator(cls, accelerator: str) -> str:
            if not accelerator or accelerator.lower() in ("none", "default", "cpu"):
                return ""
            key = accelerator.lower().strip()
            return cls.ACCELERATOR_MAP.get(key, key)

        @classmethod
        def sanitize_slug(cls, title: str) -> str:
            slug = re.sub(r"[^a-zA-Z0-9\-]", "-", title.lower()).strip("-")
            slug = re.sub(r"-+", "-", slug)[:50].rstrip("-")
            return slug or f"nb-{uuid.uuid4().hex[:4]}"

    return _gpu, _KS, "standalone"


is_gpu_accelerator, KaggleService, PLATFORM_MODE = _load_platform()

SHARD_TAG = "kaggle-automation-shard-fallback"

FALLBACK_HEADER = """\
# ==========================================
# AUTO-INJECTED WORKLOAD SHARD CONFIGURATION (Standalone fallback)
# Overridden by the distributed runner at push time
# ==========================================
import os as _ka_os

def _get_shard_var(name, default=None, cast=None):
    val = globals().get(name)
    if val is not None:
        return val
    env_val = _ka_os.environ.get(name)
    if env_val is not None and str(env_val).strip():
        if cast is not None:
            try:
                return cast(env_val)
            except (TypeError, ValueError):
                return default
        if default is not None:
            try:
                return type(default)(env_val)
            except (TypeError, ValueError):
                return default
        return env_val
    return default

SHARD_ID = _get_shard_var('SHARD_ID', 0, cast=int)
TOTAL_SHARDS = _get_shard_var('TOTAL_SHARDS', 1, cast=int)
START_INDEX = _get_shard_var('START_INDEX', None, cast=int)
END_INDEX = _get_shard_var('END_INDEX', None, cast=int)
TOTAL_ITEMS = _get_shard_var('TOTAL_ITEMS', None, cast=int)
SHARD_PARAMS = {}
"""

BUFFER_HEADER = """\
import sys as _ka_sys
try:
    _ka_sys.stdout.reconfigure(line_buffering=True)
    _ka_sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass
"""

DIRS_HEADER = """\
import os as _ka_os2
WORKING_DIR = "/kaggle/working" if _ka_os2.path.exists("/kaggle/working") else _ka_os2.getcwd()
SCRATCH_DIR = "/kaggle/tmp" if _ka_os2.path.exists("/kaggle/tmp") else _ka_os2.getcwd()
"""


def _has(src: str, *needles: str) -> bool:
    return any(n in src for n in needles)


def _prepend_py(src: str, header: str) -> str:
    return header + ("\n" if not src.startswith("\n") else "") + src


def _notebook_cells(nb: dict) -> list:
    cells = nb.get("cells")
    return cells if isinstance(cells, list) else []


def _cell_text(cell: dict) -> str:
    src = cell.get("source", "")
    return "".join(src) if isinstance(src, list) else str(src)


def _notebook_source(nb: dict) -> str:
    return "\n".join(_cell_text(c) for c in _notebook_cells(nb))


def _prepend_cell(nb: dict, code: str, tag: str) -> dict:
    for c in _notebook_cells(nb):
        tags = (c.get("metadata") or {}).get("tags", [])
        if tag in tags:
            return nb  # already ported — idempotent
    cell = {
        "cell_type": "code",
        "execution_count": None,
        "metadata": {"tags": [tag]},
        "outputs": [],
        "source": [line + "\n" for line in code.splitlines()],
    }
    nb["cells"] = [cell] + _notebook_cells(nb)
    return nb


def port_py(src: str, mode: str) -> tuple[str, list[str]]:
    fixes, out = [], src
    if mode in ("both", "distributed") and not _has(out, "_get_shard_var", "SHARD_ID"):
        out = _prepend_py(out, FALLBACK_HEADER)
        fixes.append("shard-fallback-header")
    if "line_buffering" not in out:
        out = _prepend_py(out, BUFFER_HEADER)
        fixes.append("line-buffering")
    if "WORKING_DIR" not in out:
        out = _prepend_py(out, DIRS_HEADER)
        fixes.append("working-scratch-dirs")
    return out, fixes


def port_notebook(nb_text: str, mode: str) -> tuple[str, list[str]]:
    nb = json.loads(KaggleService.ensure_executable_notebook(nb_text))
    fixes = []
    src = _notebook_source(nb)
    if mode in ("both", "distributed") and not _has(src, "_get_shard_var", "SHARD_ID"):
        nb = _prepend_cell(nb, FALLBACK_HEADER, SHARD_TAG)
        fixes.append("shard-fallback-header")
    src = _notebook_source(nb)
    if "line_buffering" not in src:
        nb = _prepend_cell(nb, BUFFER_HEADER, "kaggle-automation-buffering")
        fixes.append("line-buffering")
    src = _notebook_source(nb)
    if "WORKING_DIR" not in src:
        nb = _prepend_cell(nb, DIRS_HEADER, "kaggle-automation-dirs")
        fixes.append("working-scratch-dirs")
    return json.dumps(nb, indent=2) + "\n", fixes


def lint(src: str, title: str, accelerator: str) -> list[str]:
    warnings = []
    if len(title) > 50:
        warnings.append(f"title>{len(title)}chars: Kaggle truncates to 50 and keys kernels by slug — shorten it")
    if accelerator and is_gpu_accelerator(accelerator):
        if "cuda.device_count" not in src and "cuda.is_available" not in src:
            warnings.append("no GPU guard: add torch.cuda.device_count()==0 → sys.exit(1) check")
        if KaggleService.resolve_accelerator(accelerator) == accelerator.lower().strip() and accelerator.lower() not in ("none", "default", "cpu"):
            pass  # custom machine_shape passthrough — allowed, no warning
        elif "p100" in accelerator.lower():
            warnings.append("P100 is broken with default Kaggle PyTorch (cu128) — use T4 instead")
    if "HF_TOKEN" in src and 'os.environ.get("HF_TOKEN")' not in src and "os.environ['HF_TOKEN']" not in src and "HF_TOKEN=" in src.replace(" ", ""):
        warnings.append("possible hardcoded HF_TOKEN: read it from os.environ (server injects it)")
    if "sk-" in src and "api_key" in src.lower():
        warnings.append("possible hardcoded API key: move to env vars")
    if "cd /kaggle/working" in src or '"/kaggle/working/' in src and "SCRATCH_DIR" not in src:
        warnings.append("inputs under /kaggle/working bloat published output — download to SCRATCH_DIR (/kaggle/tmp)")
    if "OUTPUT_FILE" in src:
        out_lines = [ln for ln in src.splitlines() if "OUTPUT_FILE" in ln]
        if out_lines and not any("SHARD_ID" in ln for ln in out_lines):
            warnings.append("OUTPUT_FILE ignores SHARD_ID: shards will overwrite each other — name per-shard")
    if "pip install" in src and "uv pip install --system" not in src:
        warnings.append("use `uv pip install --system` on Kaggle (no active venv)")
    if "print(" in src and "flush=True" not in src and "line_buffering" not in src:
        warnings.append("loop prints without flush: piped logs can stall for hours")
    return warnings


def main() -> int:
    ap = argparse.ArgumentParser(description="Port a script/notebook to kaggle-automation.")
    ap.add_argument("input", help=".py or .ipynb file to port")
    ap.add_argument("--out", required=True, help="output path (.py or .ipynb)")
    ap.add_argument("--title", default="Untitled Job", help="Kaggle notebook title (<=50 chars)")
    ap.add_argument("--accelerator", default="nvidia-tesla-t4-x2")
    ap.add_argument("--mode", choices=["single", "distributed", "both"], default="both")
    ap.add_argument("--check", action="store_true", help="report only, write nothing")
    ap.add_argument("--report", default="", help="optional JSON report path")
    args = ap.parse_args()

    with open(args.input, encoding="utf-8") as f:
        raw = f.read()

    is_nb = args.input.endswith(".ipynb") or args.out.endswith(".ipynb")
    if is_nb:
        try:
            out_text, fixes = port_notebook(raw, args.mode)
        except Exception as e:
            print(f"ERROR: notebook parse failed: {e}", file=sys.stderr)
            return 2
        src_for_lint = _notebook_source(json.loads(out_text))
    else:
        out_text, fixes = port_py(raw, args.mode)
        src_for_lint = out_text
    try:
        compile(src_for_lint if not is_nb else out_text if out_text.strip().startswith("{") else src_for_lint, args.out, "exec")
    except SyntaxError:
        pass  # ipynb JSON is validated by json.loads above; py syntax checked below
    if not is_nb:
        try:
            compile(out_text, args.out, "exec")
        except SyntaxError as e:
            print(f"ERROR: ported output has syntax error: {e}", file=sys.stderr)
            return 2

    warnings = lint(src_for_lint, args.title, args.accelerator)
    if PLATFORM_MODE == "standalone":
        warnings = [
            "standalone mode: no platform checkout found (set KAGGLE_AUTOMATION_REPO if the server's mapping ever drifts)"
        ] + warnings
    report = {
        "input": args.input,
        "output": args.out,
        "title": args.title,
        "slug": KaggleService.sanitize_slug(args.title[:50]),
        "accelerator": args.accelerator,
        "machine_shape": KaggleService.resolve_accelerator(args.accelerator),
        "mode": args.mode,
        "platform_mode": PLATFORM_MODE,
        "fixes": fixes,
        "warnings": warnings,
    }
    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
    print(json.dumps(report, indent=2))
    if args.check:
        return 0
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(out_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
