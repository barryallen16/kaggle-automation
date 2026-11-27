import json
import re
import uuid

ACCELERATOR_MAP: dict[str, str] = {
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
    if not env_vars:
        return ""
    lines = [
        "import os",
    ]
    for key in sorted(env_vars):
        lines.append(f"os.environ[{key!r}] = {env_vars[key]!r}")
    lines.append("")
    text = "\n".join(lines)
    if is_notebook:
        nb = json.loads(ensure_executable_notebook(text))
        nb["cells"][0]["metadata"] = {"tags": ["kaggle-automation-env"]}
        return json.dumps(nb)
    return text + "\n"


def resolve_accelerator(accelerator: str) -> str:
    if not accelerator or accelerator.lower() in ("none", "default", "cpu"):
        return ""
    key = accelerator.lower().strip()
    return ACCELERATOR_MAP.get(key, key)


def sanitize_slug(title: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9\-]", "-", title.lower()).strip("-")
    slug = re.sub(r"-+", "-", slug)[:50].rstrip("-")
    return slug or f"nb-{uuid.uuid4().hex[:4]}"


def ensure_executable_notebook(code_content: str) -> str:
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
    s = (raw or "").strip().lower()
    if not s:
        return "unknown"
    if "cancel_acknowledged" in s or "cancel-acknowledged" in s:
        return "cancelling"
    if "cancel" in s:
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


TERMINAL_KERNEL_STATUSES = ("complete", "error", "stopped", "canceled", "cancelling")

_OUTDATED_CLI_RE = re.compile(
    r"^Warning:\s*Looks like you're using an outdated [`']?kaggle[`']? version\b[^\r\n]*(?:\r?\n|$)",
    re.IGNORECASE | re.MULTILINE,
)


def strip_kaggle_cli_warnings(text: str) -> str:
    if not text or "outdated" not in text.lower():
        return text
    return _OUTDATED_CLI_RE.sub("", text)
