"""Kernel identity + notebook normalization (pure helpers, no I/O).

Deep module extracted from KaggleService: small interface (five functions),
all kernel slug/accelerator/notebook/status derivation lives here.
KaggleService keeps thin delegating aliases so existing callers
(routers, distributor, port-to-kaggle skill, tests) keep working.
"""

import json
import re
import uuid

# Mapping from user-friendly accelerator names to Kaggle API machine_shape enum values.
# Full list: https://github.com/Kaggle/kaggle-cli/blob/main/docs/kernels_metadata.md
# WARNING: NvidiaTeslaP100 is broken with default Kaggle image PyTorch (cu128) - avoid.
ACCELERATOR_MAP: dict[str, str] = {
    # T4 variants (gives 2x T4 by default)
    "nvidia-tesla-t4": "NvidiaTeslaT4",
    "nvidia-tesla-t4-x2": "NvidiaTeslaT4",
    "t4": "NvidiaTeslaT4",
    "t4-x2": "NvidiaTeslaT4",
    "gpu-tesla-t4": "NvidiaTeslaT4",
    "gpu-tesla-t4-x2": "NvidiaTeslaT4",
    # T4 High Memory
    "nvidia-tesla-t4-highmem": "NvidiaTeslaT4Highmem",
    "t4-highmem": "NvidiaTeslaT4Highmem",
    "t4highmem": "NvidiaTeslaT4Highmem",
    # P100 (broken with PyTorch cu128 - use T4 instead)
    "nvidia-tesla-p100": "NvidiaTeslaP100",
    "gpu-p100": "NvidiaTeslaP100",
    "p100": "NvidiaTeslaP100",
    # A100
    "a100": "NvidiaTeslaA100",
    "nvidia-a100": "NvidiaTeslaA100",
    # L4
    "l4": "NvidiaL4",
    "nvidia-l4": "NvidiaL4",
    "l4x1": "NvidiaL4X1",
    "nvidia-l4-x1": "NvidiaL4X1",
    # H100
    "h100": "NvidiaH100",
    "nvidia-h100": "NvidiaH100",
    # RTX Pro 6000
    "rtx-pro-6000": "NvidiaRtxPro6000",
    "nvidia-rtx-pro-6000": "NvidiaRtxPro6000",
    # TPU variants
    "v3-8": "TpuV38",
    "tpu-v3-8": "TpuV38",
    "tpu1vm-v3-8": "Tpu1VmV38",
    "tpu1vmv38": "Tpu1VmV38",
    "tpu-v5e-8": "TpuV5E8",
    "tpu-v5e8": "TpuV5E8",
    "tpu-v6e-8": "TpuV6E8",
    "tpu-v6e8": "TpuV6E8",
}

DEFAULT_KERNELSPEC: dict[str, str] = {
    "name": "python3",
    "display_name": "Python 3",
    "language": "python",
}


def build_env_preamble(env_vars: dict[str, str], is_notebook: bool) -> str:
    """Renders os.environ assignments for kernel injection.

    Note: values become part of the private kernel's source on Kaggle.
    Fine for single-operator dashboards; don't inject shared-write secrets.
    """
    if not env_vars:
        return ""
    lines = [
        "# ==========================================",
        "# AUTO-INJECTED ENVIRONMENT (Kaggle Automation Dashboard)",
        "# ==========================================",
        "import os",
    ]
    for key in sorted(env_vars):
        lines.append(f"os.environ[{key!r}] = {env_vars[key]!r}")
    lines.append("")
    text = "\n".join(lines)
    if is_notebook:
        nb = json.loads(ensure_executable_notebook(text))
        cell_src = nb["cells"][0]["source"]
        if isinstance(cell_src, list):
            cell_src = "".join(cell_src)
        nb["cells"] = [
            {
                "cell_type": "code",
                "execution_count": None,
                "metadata": {"tags": ["kaggle-automation-env"]},
                "outputs": [],
                "source": cell_src,
            }
        ] + nb["cells"]
        return json.dumps(nb)
    return text + "\n"


def resolve_accelerator(accelerator: str) -> str:
    """Maps a user-friendly accelerator name to the correct Kaggle CLI accelerator ID."""
    if not accelerator or accelerator.lower() in ("none", "default", "cpu"):
        return ""
    key = accelerator.lower().strip()
    return ACCELERATOR_MAP.get(key, key)


def sanitize_slug(title: str) -> str:
    """Derives the exact Kaggle kernel slug from a title.

    Kaggle resolves notebooks by the slugified title - any extra suffix in
    the metadata 'id' makes the id point to a non-existent kernel while the
    title maps to an existing one, which surfaces as persistent 409
    Conflicts on every re-push. The slug must therefore match Kaggle's own
    title->slug derivation exactly.
    """
    slug = re.sub(r"[^a-zA-Z0-9\-]", "-", title.lower()).strip("-")
    slug = re.sub(r"-+", "-", slug)[:50].rstrip("-")
    return slug or f"nb-{uuid.uuid4().hex[:4]}"


def ensure_executable_notebook(code_content: str) -> str:
    """Normalizes .ipynb payloads so Kaggle can execute them.

    Kaggle's runner (papermill) requires valid notebook JSON *and* a
    metadata.kernelspec entry; otherwise the run dies at startup with
    'No kernel name found in notebook and no override provided.'.
    - Valid notebook without kernelspec -> inject the default python3 spec.
    - Raw python source mislabeled as .ipynb -> wrapped into a real notebook.
    """
    try:
        nb = json.loads(code_content)
        if not isinstance(nb, dict):
            raise TypeError("notebook JSON root must be an object")
    except Exception:
        source_lines = code_content.splitlines(keepends=True)
        nb = {
            "cells": [
                {
                    "cell_type": "code",
                    "execution_count": None,
                    "metadata": {},
                    "outputs": [],
                    "source": source_lines,
                }
            ],
            "metadata": {},
            "nbformat": 4,
            "nbformat_minor": 2,
        }

    metadata = nb.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    metadata.setdefault("kernelspec", dict(DEFAULT_KERNELSPEC))
    nb["metadata"] = metadata

    if not isinstance(nb.get("cells"), list):
        nb["cells"] = []
    nb.setdefault("nbformat", 4)
    nb.setdefault("nbformat_minor", 2)

    return json.dumps(nb)


def normalize_kernel_status(raw: str) -> str:
    """Maps every observed CLI/SDK spelling onto our five statuses.

    Newer CLIs emit enum names like 'kernelworkerstatus.cancel_acknowledged'
    which previously leaked into the DB verbatim and defeated terminal-state
    detection (runs looked neither complete nor stopped forever).
    """
    s = (raw or "").strip().lower()
    if not s:
        return "unknown"
    if "cancel" in s:  # canceled / cancelled / cancel_acknowledged
        return "stopped"
    if "complete" in s:
        return "complete"
    if "error" in s or "fail" in s:
        return "error"
    if "running" in s:
        return "running"
    if "queued" in s:
        return "queued"
    return "unknown"
