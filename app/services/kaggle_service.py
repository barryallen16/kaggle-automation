import os
import io
import csv
import json
import sys
import uuid
import shutil
import asyncio
import logging
import re
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional, AsyncGenerator
from app.config import NOTEBOOKS_DIR, LOGS_DIR, OUTPUTS_DIR, get_kaggle_cli_path, get_kernel_env_defaults
from app.services.account_manager import AccountManager
from app.services.ops_tracker import tracker, run_stop_key
from app.database import create_run_record, update_run_status, get_run_by_id, get_active_runs, set_run_output_version, utcnow_iso

logger = logging.getLogger("kaggle_service")

# Helper that talks to kagglesdk with version_label set - the plain CLI
# downloads only the LATEST kernel output and silently drops the version.
VERSIONED_OUTPUT_HELPER = Path(__file__).resolve().parent / "kaggle_versioned_output.py"
VERSIONED_FETCH_TIMEOUT_SECONDS = int(os.getenv("VERSIONED_FETCH_TIMEOUT_SECONDS", "600"))

class KaggleService:
    # Seconds to wait before each attempt to fetch the cancelled version's
    # output after a stop push (Kaggle finalizes the version asynchronously).
    STOP_CAPTURE_RETRY_DELAYS = (6, 12, 20)
    # Active log stream processes: run_id -> asyncio.subprocess.Process
    _active_stream_processes: Dict[str, asyncio.subprocess.Process] = {}
    # Subscribers for live log broadcasting: run_id -> List[asyncio.Queue]
    _log_subscribers: Dict[str, List[asyncio.Queue]] = {}

    # ---- OOM guard: kaggle CLI processes are ~50-150MB each. 16 parallel
    # pushes (16 accounts x2 sessions) spike to >2GB and the OOM-killer
    # kills uvicorn ("killed uvicorn ..."). Same for status checks.
    PUSH_CONCURRENCY_LIMIT = max(1, int(os.getenv("PUSH_CONCURRENCY", "3")))
    KERNEL_STATUS_CONCURRENCY_LIMIT = max(1, int(os.getenv("KERNEL_STATUS_CONCURRENCY", "3")))
    KERNEL_STATUS_TIMEOUT_SECONDS = int(os.getenv("KERNEL_STATUS_TIMEOUT_SECONDS", "90"))
    # Stop pushes are even heavier than regular pushes: each stop_kernel runs a
    # pre-stop output pull + a version probe + the stop-stub push (+ retries).
    # "Stop All" on a 32-shard workload used to fire ALL of them at once -> RAM
    # spike -> OOM-killed the server. All stops funnel through this global cap.
    STOP_CONCURRENCY_LIMIT = max(1, int(os.getenv("STOP_CONCURRENCY", "3")))
    _push_semaphore: Optional[asyncio.Semaphore] = None
    _kernel_status_semaphore: Optional[asyncio.Semaphore] = None
    _stop_semaphore: Optional[asyncio.Semaphore] = None
    _push_primitive_loop_id: Optional[int] = None

    @classmethod
    def _get_push_semaphore(cls) -> asyncio.Semaphore:
        loop_id = id(asyncio.get_running_loop())
        if cls._push_semaphore is None or cls._push_primitive_loop_id != loop_id:
            cls._push_semaphore = asyncio.Semaphore(cls.PUSH_CONCURRENCY_LIMIT)
            cls._push_primitive_loop_id = loop_id
        return cls._push_semaphore

    @classmethod
    def _get_kernel_status_semaphore(cls) -> asyncio.Semaphore:
        loop_id = id(asyncio.get_running_loop())
        if cls._kernel_status_semaphore is None or cls._push_primitive_loop_id != loop_id:
            cls._kernel_status_semaphore = asyncio.Semaphore(cls.KERNEL_STATUS_CONCURRENCY_LIMIT)
            cls._push_primitive_loop_id = loop_id
        return cls._kernel_status_semaphore

    @classmethod
    def _get_stop_semaphore(cls) -> asyncio.Semaphore:
        loop_id = id(asyncio.get_running_loop())
        if cls._stop_semaphore is None or cls._push_primitive_loop_id != loop_id:
            cls._stop_semaphore = asyncio.Semaphore(cls.STOP_CONCURRENCY_LIMIT)
            cls._push_primitive_loop_id = loop_id
        return cls._stop_semaphore

    # Mapping from user-friendly accelerator names to Kaggle API machine_shape enum values.
    # Full list: https://github.com/Kaggle/kaggle-cli/blob/main/docs/kernels_metadata.md
    # WARNING: NvidiaTeslaP100 is broken with default Kaggle image PyTorch (cu128) - avoid.
    ACCELERATOR_MAP = {
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

    @classmethod
    def build_env_preamble(cls, env_vars: Dict[str, str], is_notebook: bool) -> str:
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
            nb = json.loads(cls.ensure_executable_notebook(text))
            cell_src = nb["cells"][0]["source"]
            if isinstance(cell_src, list):
                cell_src = "".join(cell_src)
            nb["cells"] = [{
                "cell_type": "code",
                "execution_count": None,
                "metadata": {"tags": ["kaggle-automation-env"]},
                "outputs": [],
                "source": cell_src
            }] + nb["cells"]
            return json.dumps(nb)
        return text + "\n"

    @classmethod
    def resolve_accelerator(cls, accelerator: str) -> str:
        """Maps a user-friendly accelerator name to the correct Kaggle CLI accelerator ID."""
        if not accelerator or accelerator.lower() in ("none", "default", "cpu"):
            return ""
        key = accelerator.lower().strip()
        return cls.ACCELERATOR_MAP.get(key, key)

    @classmethod
    def sanitize_slug(cls, title: str) -> str:
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

    DEFAULT_KERNELSPEC = {
        "name": "python3",
        "display_name": "Python 3",
        "language": "python"
    }

    @classmethod
    def ensure_executable_notebook(cls, code_content: str) -> str:
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
                raise ValueError("notebook JSON root must be an object")
        except Exception:
            source_lines = code_content.splitlines(keepends=True)
            nb = {
                "cells": [{
                    "cell_type": "code",
                    "execution_count": None,
                    "metadata": {},
                    "outputs": [],
                    "source": source_lines
                }],
                "metadata": {},
                "nbformat": 4,
                "nbformat_minor": 2
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
    def _purge_previous_logs(cls) -> None:
        """Wipes data/logs/ so each new launch starts with a clean log directory.

        Best-effort per file: a log currently held open by a live follower
        (Windows locks open files) is simply skipped and will age out later.
        """
        try:
            LOGS_DIR.mkdir(parents=True, exist_ok=True)
            for f in LOGS_DIR.iterdir():
                try:
                    if f.is_file():
                        f.unlink()
                    elif f.is_dir():
                        shutil.rmtree(f, ignore_errors=True)
                except OSError:
                    continue
        except Exception as e:
            logger.warning(f"Could not purge old logs: {e}")

    @classmethod
    async def push_kernel(
        cls,
        account_username: str,
        title: str,
        code_content: str,
        filename: str = "notebook.ipynb",
        accelerator: str = "none",
        enable_internet: bool = True,
        is_trial: bool = False,
        timeout_seconds: Optional[int] = None,
        env_vars: Optional[Dict[str, str]] = None,
        workload_id: Optional[str] = None,
        shard_index: Optional[int] = None,
        total_shards: Optional[int] = None
    ) -> Dict[str, Any]:
        """Prepares metadata, writes code, and executes `kaggle kernels push`."""
        # Resolve stale/renamed account names up front: when the first push of
        # a fresh account discovers the real Kaggle username it renames the row
        # mid-batch, and later shards of the same launch still arrive with the
        # placeholder (kaggle_xxxx) name. Resolving here keeps kernel_ref, the
        # metadata id and the subprocess env all on the CURRENT identity.
        account_username = AccountManager.resolve_effective_username(account_username)
        run_hash = uuid.uuid4().hex[:6]
        run_id = f"run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{run_hash}"
        # Ensure title fits Kaggle's 50-character limit; derive the slug from
        # exactly what we send as the title. Kaggle keys kernels by the
        # slugified title - a mismatched metadata id makes every re-push 409.
        clean_title = title[:50]
        slug = cls.sanitize_slug(clean_title)
        kernel_ref = f"{account_username}/{slug}"
        kaggle_url = f"https://www.kaggle.com/code/{kernel_ref}"

        # Guard: Kaggle keys notebooks by title, so launching while another run
        # of the SAME kernel is queued/running would silently replace it - and
        # stopping one row would kill both. Block with an actionable error.
        for r in get_active_runs():
            if r["kernel_ref"] == kernel_ref:
                return {
                    "success": False,
                    "status": "conflict",
                    "error": (
                        f"'{clean_title}' is already {r['status']} on this account "
                        f"(run {r['id']}). Stop it first, wait for it to finish, "
                        "or use a different title."
                    ),
                    "conflict_run_id": r["id"],
                    "run_id": None
                }
        
        # Working folder for this run
        run_dir = NOTEBOOKS_DIR / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        
        # Write notebook / script file
        is_notebook = filename.endswith(".ipynb")
        kernel_type = "notebook" if is_notebook else "script"

        # Inject environment secrets: explicit per-request vars override the
        # .env defaults (HF_TOKEN etc.). Applied before any user code runs.
        effective_env = {**get_kernel_env_defaults(), **(env_vars or {})}
        preamble = cls.build_env_preamble(effective_env, is_notebook)

        if is_notebook:
            code_content = cls.ensure_executable_notebook(code_content)
            if preamble:
                nb = json.loads(code_content)
                env_nb = json.loads(preamble)
                nb["cells"] = env_nb["cells"] + nb["cells"]
                code_content = json.dumps(nb)
        elif preamble:
            code_content = preamble + code_content

        code_path = run_dir / filename
        with open(code_path, "w", encoding="utf-8") as f:
            f.write(code_content)
        
        # Accelerator flags
        # Resolve machine shape first so enable_gpu/enable_tpu stay consistent
        # with it even for names lacking the 'gpu'/'tpu' substrings (l4/a100/h100...)
        resolved_machine = cls.resolve_accelerator(accelerator)
        machine_lower = (resolved_machine or accelerator).lower()
        enable_gpu = "true" if ("gpu" in machine_lower or "nvidia" in machine_lower or "t4" in machine_lower) else "false"
        enable_tpu = "true" if "tpu" in machine_lower else "false"
        
        metadata = {
            "id": kernel_ref,
            "title": clean_title,
            "code_file": filename,
            "language": "python",
            "kernel_type": kernel_type,
            "is_private": "true",
            "enable_gpu": enable_gpu,
            "enable_tpu": enable_tpu,
            "enable_internet": "true" if enable_internet else "false",
            "machine_shape": resolved_machine or "",
            "dataset_sources": [],
            "competition_sources": [],
            "kernel_sources": [],
            "model_sources": []
        }
        
        meta_path = run_dir / "kernel-metadata.json"
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2)

        # Build CLI command
        cli = get_kaggle_cli_path()
        cmd = [cli, "kernels", "push", "-p", str(run_dir)]
        
        # Accelerator CLI option (map user-friendly name to Kaggle CLI ID)
        resolved_acc = cls.resolve_accelerator(accelerator)
        if resolved_acc:
            cmd.extend(["--accelerator", resolved_acc])
        
        # Timeout handling (for trial run or custom timeout)
        effective_timeout = timeout_seconds or (300 if is_trial else 43200)
        if effective_timeout:
            cmd.extend(["-t", str(effective_timeout)])

        # Log purge is now done once per distributed workload (WorkloadDistributor),
        # not per shard. Per-shard purge raced when 16-32 shards launched in
        # parallel and also deleted logs of shards that just started streaming.
        # Keep purge for single-run pushes only when caller hasn't already purged.
        # WorkloadDistributor sets _WORKLOAD_PURGE_DONE env flag per launch batch.
        if not os.getenv("_WORKLOAD_PURGE_DONE"):
            cls._purge_previous_logs()

        log_file_path = LOGS_DIR / f"{run_id}.log"
        log_file_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_file_path, "w", encoding="utf-8") as f:
            f.write(f"=== Kaggle Run Initialized: {utcnow_iso()} ===\n")
            f.write(f"Kernel: {kernel_ref}\n")
            f.write(f"Title: {title}\n")
            f.write(f"Accelerator: {accelerator}\n")
            f.write(f"Trial Mode: {is_trial} (Timeout: {effective_timeout}s)\n")
            f.write(f"Command: {' '.join(cmd)}\n\n")

        env = AccountManager.get_account_env(account_username)
        
        # Retry logic for 409 Conflict errors (Kaggle rate-limits when a kernel
        # is still starting/running on the same account) and transient batch-GPU
        # session caps. The two need VERY different windows:
        #   - session cap: a second same-account GPU session usually has to wait
        #     out the first session's Kaggle queue (often minutes), and a stop
        #     teardown (CANCEL_ACKNOWLEDGED) can hold the slot for many minutes.
        #     Keep retrying ~18 minutes so 2 sessions/account actually land.
        #   - 409 conflict: clears in seconds once the kernel finishes its
        #     transition - retry briefly, never burn the long window on it.
        MAX_RETRIES = 4
        RETRY_BASE_DELAY = 5  # seconds
        SESSION_CAP_RETRY_DELAYS = [20, 30, 45, 60, 90, 120, 180, 240, 300]
        total_attempts = max(MAX_RETRIES, len(SESSION_CAP_RETRY_DELAYS) + 1)
        
        out_str = ""
        err_str = ""
        proc = None
        
        try:
            for attempt in range(total_attempts):
                async with cls._get_push_semaphore():
                    proc = await asyncio.create_subprocess_exec(
                        *cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        env=env
                    )
                    stdout, stderr = await proc.communicate()
                out_str = stdout.decode("utf-8", errors="ignore")
                err_str = stderr.decode("utf-8", errors="ignore")

                # Retryable push failures: 409 Conflict (kernel transitioning)
                # and the transient batch-GPU-session cap while old sessions reap.
                # The CLI prints "Kernel push error: ..." on STDOUT - scan BOTH streams.
                combined_out = out_str + "\n" + err_str
                low = combined_out.lower()
                session_cap_hit = "maximum batch gpu session count" in low
                is_409 = "409" in combined_out or "conflict" in low
                is_push_err = proc.returncode != 0 or "kernel push error" in low
                retryable = is_push_err and (is_409 or session_cap_hit)
                if retryable:
                    if session_cap_hit:
                        delay = SESSION_CAP_RETRY_DELAYS[min(attempt, len(SESSION_CAP_RETRY_DELAYS) - 1)]
                        retry_limit = total_attempts  # long window: let the first session's queue settle
                    else:
                        delay = RETRY_BASE_DELAY * (2 ** attempt)
                        retry_limit = MAX_RETRIES  # 409s clear fast - don't burn 25 min on them
                    if attempt >= retry_limit - 1:
                        break
                    reason = "session cap" if session_cap_hit else "409 conflict"
                    logger.warning(f"Kaggle push attempt {attempt + 1}/{retry_limit} hit {reason}, backing off {delay}s...")
                    with open(log_file_path, "a", encoding="utf-8") as f:
                        f.write(f"[RETRY] attempt {attempt + 1} failed ({reason}), backing off {delay}s...\n")
                    await asyncio.sleep(delay)
                    continue
                break

            with open(log_file_path, "a", encoding="utf-8") as f:
                if out_str:
                    f.write(f"[OUTPUT]\n{out_str}\n")
                if err_str:
                    f.write(f"[STDERR]\n{err_str}\n")

            # A run only fails if the CLI itself failed or Kaggle explicitly reported
            # a push error. Never infer failure from arbitrary words in stderr.
            is_error = proc.returncode != 0 or "Kernel push error" in out_str
            status = "error" if is_error else "queued"
            status_msg = err_str if is_error else out_str.strip()

            # Parse real Kaggle URL, username, and slug from push output if available
            url_match = re.search(r'https?://(?:www\.)?kaggle\.com/code/([a-zA-Z0-9_\-]+)/([a-zA-Z0-9_\-]+)', out_str)
            if url_match:
                real_username = url_match.group(1)
                real_slug = url_match.group(2)
                kernel_ref = f"{real_username}/{real_slug}"
                kaggle_url = f"https://www.kaggle.com/code/{kernel_ref}"
                # Auto-correct account username in DB if it was mismatched
                if real_username != account_username:
                    logger.info(f"Auto-corrected account username from {account_username} to {real_username}")
                    AccountManager.update_username(account_username, real_username)
                    account_username = real_username

            run_record = {
                "id": run_id,
                "account_username": account_username,
                "kernel_slug": slug if not url_match else real_slug,
                "kernel_ref": kernel_ref,
                "title": title,
                "code_file": str(code_path),
                "accelerator": accelerator,
                "enable_internet": 1 if enable_internet else 0,
                "is_trial": 1 if is_trial else 0,
                "timeout_seconds": effective_timeout,
                "status": status,
                "status_message": status_msg,
                "start_time": utcnow_iso(),
                "kaggle_url": kaggle_url,
                "workload_id": workload_id,
                "shard_index": shard_index,
                "total_shards": total_shards,
                "log_file": str(log_file_path)
            }
            create_run_record(run_record)

            # Start background log streaming if queued/started successfully
            # Cap concurrent followers - 32 shards each holding a `kaggle logs -f`
            # subprocess is the same OOM spike as pushes. Beyond the cap, logs are
            # still collected on demand when the user opens the Logs WebSocket
            # (see logs.py:85 ensure_log_stream).
            if not is_error:
                max_streams = max(1, int(os.getenv("MAX_CONCURRENT_LOG_STREAMS", "6")))
                if len(cls._active_stream_processes) < max_streams:
                    asyncio.create_task(cls.start_background_log_stream(run_id, account_username, kernel_ref, log_file_path))
                else:
                    logger.info(f"Log streaming deferred for {run_id} ({len(cls._active_stream_processes)}/{max_streams} streams active) - will start on demand")

            return {
                "success": not is_error,
                "run_id": run_id,
                "kernel_ref": kernel_ref,
                "kaggle_url": kaggle_url,
                "status": status,
                "message": status_msg
            }
        except Exception as e:
            logger.error(f"Failed to push kernel {kernel_ref}: {e}")
            with open(log_file_path, "a", encoding="utf-8") as f:
                f.write(f"\n[EXCEPTION] {str(e)}\n")
            return {
                "success": False,
                "run_id": run_id,
                "error": str(e)
            }

    @staticmethod
    def _normalize_kernel_status(raw: str) -> str:
        """Maps every observed CLI/SDK spelling onto our five statuses.

        Newer CLIs emit enum names like 'kernelworkerstatus.cancel_acknowledged'
        which previously leaked into the DB verbatim and defeated terminal-state
        detection (runs looked neither complete nor stopped forever).
        """
        s = (raw or "").strip().lower()
        if not s:
            return "unknown"
        if "cancel" in s:          # canceled / cancelled / cancel_acknowledged
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

    @classmethod
    async def get_kernel_status(cls, account_username: str, kernel_ref: str) -> Dict[str, Any]:
        """Queries `kaggle kernels status <kernel_ref>` - throttled + bounded."""
        cli = get_kaggle_cli_path()
        cmd = [cli, "kernels", "status", kernel_ref]
        env = AccountManager.get_account_env(account_username)

        try:
            async with cls._get_kernel_status_semaphore():
                proc = await asyncio.wait_for(
                    asyncio.create_subprocess_exec(
                        *cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        env=env
                    ),
                    timeout=cls.KERNEL_STATUS_TIMEOUT_SECONDS
                )
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=cls.KERNEL_STATUS_TIMEOUT_SECONDS
                )
            out_str = stdout.decode("utf-8", errors="ignore").strip()

            # Status parsing: e.g. 'username/slug has status "running"'
            status_match = re.search(r'status "(.*?)"', out_str, re.IGNORECASE)
            captured = status_match.group(1) if status_match else out_str
            status = cls._normalize_kernel_status(captured)

            return {
                "success": True,
                "raw": out_str,
                "status": status
            }
        except asyncio.TimeoutError:
            logger.warning(f"get_kernel_status timed out for {kernel_ref} (@{account_username})")
            return {"success": False, "status": "unknown", "error": "timeout"}
        except Exception as e:
            return {"success": False, "status": "unknown", "error": str(e)}

    @classmethod
    async def fetch_full_logs(cls, account_username: str, kernel_ref: str) -> str:
        """Fetches the latest execution logs using `kaggle kernels logs <kernel_ref>`."""
        cli = get_kaggle_cli_path()
        cmd = [cli, "kernels", "logs", kernel_ref]
        env = AccountManager.get_account_env(account_username)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env
            )
            stdout, stderr = await proc.communicate()
            logs = stdout.decode("utf-8", errors="ignore")
            err = stderr.decode("utf-8", errors="ignore")
            return logs if logs else err
        except Exception as e:
            return f"Error fetching logs: {str(e)}"

    @classmethod
    async def start_background_log_stream(cls, run_id: str, account_username: str, kernel_ref: str, log_file: Path):
        """Follows kernel logs and survives follower failures until the run ends.

        Two failure modes used to kill logging silently:
        1. stderr was piped but never drained - a full OS pipe buffer blocks the
           kaggle CLI mid-write and stdout falls silent forever. Now drained.
        2. A dead follower (API throttle/error) ended streaming permanently.
           Now restarted with backoff until the kernel reaches a terminal state.
        """
        RETRY_DELAYS = [3, 5, 10, 20, 30, 60]
        failure_count = 0

        async def drain_stderr(stream):
            try:
                while True:
                    chunk = await stream.read(4096)
                    if not chunk:
                        return
            except Exception:
                return

        def append_and_broadcast(text: str):
            try:
                with open(log_file, "a", encoding="utf-8") as f:
                    f.write(text)
                    f.flush()
            except OSError:
                pass
            for q in list(cls._log_subscribers.get(run_id, [])):
                q.put_nowait(text)

        first_attach = True
        try:
            while True:
                cli = get_kaggle_cli_path()
                cmd = [cli, "kernels", "logs", "-f", "--interval", "10", kernel_ref]
                env = AccountManager.get_account_env(account_username)

                proc = None
                drainer = None
                produced = False
                rc = None
                try:
                    proc = await asyncio.create_subprocess_exec(
                        *cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        env=env
                    )
                    cls._active_stream_processes[run_id] = proc
                    drainer = asyncio.create_task(drain_stderr(proc.stderr))

                    if first_attach:
                        append_and_broadcast(f"\n--- Live Stream Connected [{utcnow_iso()}] ---\n")
                        first_attach = False
                    else:
                        append_and_broadcast(f"\n--- Live Stream Re-attached [{utcnow_iso()}] ---\n")

                    while True:
                        line = await proc.stdout.readline()
                        if not line:
                            break
                        produced = True
                        append_and_broadcast(line.decode("utf-8", errors="ignore"))
                    rc = await proc.wait()
                finally:
                    if drainer:
                        drainer.cancel()
                    if cls._active_stream_processes.get(run_id) is proc:
                        del cls._active_stream_processes[run_id]
                    if proc and proc.returncode is None:
                        try:
                            proc.kill()
                        except Exception:
                            pass

                # Terminal kernel state? Nothing more will ever arrive.
                status_resp = await cls.get_kernel_status(account_username, kernel_ref)
                status = status_resp.get("status", "unknown")
                if status in ("complete", "error", "stopped"):
                    append_and_broadcast(f"\n--- Stream ended: kernel {status} [{utcnow_iso()}] ---\n")
                    return

                if produced:
                    failure_count = 0
                delay = RETRY_DELAYS[min(failure_count, len(RETRY_DELAYS) - 1)]
                failure_count += 1
                append_and_broadcast(
                    f"\n[STREAM] follower exited (rc={rc}, kernel={status}); reconnecting in {delay}s...\n"
                )
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Log streaming ended with error for {run_id}: {e}")
            append_and_broadcast(f"\n[STREAM] terminated with error: {e}\n")
        finally:
            cls._active_stream_processes.pop(run_id, None)

    @classmethod
    def ensure_log_stream(cls, run: Dict[str, Any]) -> None:
        """(Re)starts the background log follower for an active run if none is alive.

        Self-healing for streamers lost to server restarts or crashes: opening
        the Logs view (or the WebSocket) on an active run revives the producer
        without re-pushing anything.
        """
        run_id = run.get("id")
        if not run_id:
            return
        existing = cls._active_stream_processes.get(run_id)
        if existing is not None and existing.returncode is None:
            return  # a follower is already alive
        if run.get("status") not in ("queued", "running"):
            return  # finished runs have no live output
        log_file = run.get("log_file")
        if not log_file:
            return
        asyncio.create_task(
            cls.start_background_log_stream(
                run_id, run["account_username"], run["kernel_ref"], Path(log_file)
            )
        )

    @classmethod
    def register_log_subscriber(cls, run_id: str) -> asyncio.Queue:
        queue = asyncio.Queue()
        if run_id not in cls._log_subscribers:
            cls._log_subscribers[run_id] = []
        cls._log_subscribers[run_id].append(queue)
        return queue

    @classmethod
    def unregister_log_subscriber(cls, run_id: str, queue: asyncio.Queue):
        if run_id in cls._log_subscribers and queue in cls._log_subscribers[run_id]:
            cls._log_subscribers[run_id].remove(queue)
            if not cls._log_subscribers[run_id]:
                del cls._log_subscribers[run_id]

    @classmethod
    async def list_output_files(cls, account_username: str, kernel_ref: str) -> List[Dict[str, Any]]:
        """Lists output files generated by the kernel run."""
        cli = get_kaggle_cli_path()
        cmd = [cli, "kernels", "files", "-v", kernel_ref]
        env = AccountManager.get_account_env(account_username)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env
            )
            stdout, stderr = await proc.communicate()
            out_str = stdout.decode("utf-8", errors="ignore")
            
            files = []
            if out_str:
                reader = csv.DictReader(io.StringIO(out_str))
                for row in reader:
                    files.append(row)
            return files
        except Exception as e:
            logger.error(f"Failed to list output files for {kernel_ref}: {e}")
            return []

    @classmethod
    async def _run_versioned_helper(cls, account_username: str, args: List[str],
                                    timeout: int = VERSIONED_FETCH_TIMEOUT_SECONDS,
                                    ok_codes: tuple = (0,)) -> Optional[str]:
        """Runs the versioned-output helper under the account's credentials.

        Returns parsed stdout (JSON) or None on any failure - the helper is a
        best-effort enhancement, never a hard dependency.
        """
        env = AccountManager.get_account_env(account_username)
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(VERSIONED_OUTPUT_HELPER), *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("Versioned-output helper timed out for @%s (%s)", account_username, args)
            return None
        except Exception as e:
            logger.warning(f"Versioned-output helper could not start: {e}")
            return None

        if proc.returncode not in ok_codes:
            tail = stderr.decode("utf-8", errors="ignore").strip()[-300:]
            logger.info(f"Versioned-output helper failed (rc={proc.returncode}): {tail}")
            return None
        return stdout.decode("utf-8", errors="ignore").strip()

    @classmethod
    async def list_account_kernels(cls, account_username: str, search: str = "", page: int = 1, page_size: int = 20) -> Dict[str, Any]:
        """Lists kernels visible to account_username via ListKernels API.

        Returns {kernels: [...], nextPageToken: str}. Each kernel has
        ref, title, slug, author, lastRunTime, currentVersionNumber etc.
        Throttled via kernel-status semaphore (same CLI weight class).
        Results are sorted reverse-chronological (recent lastRunTime first).
        """
        # Clamp to Kaggle sane limits
        page = max(1, int(page or 1))
        page_size = max(1, min(100, int(page_size or 20)))
        out = await cls._run_versioned_helper(
            account_username,
            ["list", account_username, search or "", str(page), str(page_size)],
            timeout=120
        )
        if not out:
            return {"kernels": [], "nextPageToken": ""}
        try:
            data = json.loads(out)
            kernels = data.get("kernels") or []
            # Reverse chronological: newest lastRunTime first
            def _time_key(k):
                t = k.get("lastRunTime") or k.get("last_run_time") or k.get("creationTime") or ""
                try:
                    # Use string compare for ISO; fallback to 0
                    return t or ""
                except Exception:
                    return ""
            try:
                kernels.sort(key=_time_key, reverse=True)
            except Exception:
                pass
            return {"kernels": kernels, "nextPageToken": data.get("nextPageToken") or ""}
        except Exception:
            return {"kernels": [], "nextPageToken": ""}

    @classmethod
    async def list_kernel_versions(cls, account_username: str, kernel_ref: str, max_versions: int = 20) -> Dict[str, Any]:
        """Lists per-version snapshots for a kernel, newest first.

        Each version entry has version, label, creationTime (ISO or ""), fileCount, status, hasOutput.
        """
        owner, _, slug = kernel_ref.partition("/")
        if not owner or not slug:
            return {"versions": [], "current_version": None}
        out = await cls._run_versioned_helper(
            account_username,
            ["versions", owner, slug, str(max_versions)],
            timeout=180
        )
        if not out:
            return {"versions": [], "current_version": None}
        try:
            data = json.loads(out)
            return {"versions": data.get("versions") or [], "current_version": data.get("current_version")}
        except Exception:
            return {"versions": [], "current_version": None}

    @classmethod
    async def fetch_version_log(cls, account_username: str, kernel_ref: str, version: int) -> str:
        """Fetches log for a specific version snapshot."""
        owner, _, slug = kernel_ref.partition("/")
        if not owner or not slug:
            return ""
        out = await cls._run_versioned_helper(
            account_username,
            ["log", owner, slug, str(version)],
            timeout=60
        )
        if not out:
            return ""
        try:
            return json.loads(out).get("log") or ""
        except Exception:
            return ""

    @classmethod
    async def get_kernel_current_version(cls, account_username: str, kernel_ref: str) -> Optional[int]:
        """Latest pushed version number of the kernel, or None if unknown."""
        owner, _, slug = kernel_ref.partition("/")
        if not owner or not slug:
            return None
        out = await cls._run_versioned_helper(account_username, ["meta", owner, slug], timeout=120)
        if not out:
            return None
        try:
            v = json.loads(out).get("current_version_number")
            return int(v) if v else None
        except Exception:
            return None

    @classmethod
    async def download_outputs_of_version(cls, account_username: str, kernel_ref: str, version: int, run_id: str) -> Optional[Path]:
        """Downloads a SPECIFIC version's output snapshot into data/outputs/{run_id}.

        This recovers the partial /kaggle/working contents of cancelled or
        errored versions - which latest-only pulls miss once a stop-stub or a
        newer push becomes the kernel's latest version.
        """
        owner, _, slug = kernel_ref.partition("/")
        if not owner or not slug:
            return None
        target_dir = OUTPUTS_DIR / str(run_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        # rc 0 = files saved; rc 3 = version finalized but published nothing
        out = await cls._run_versioned_helper(
            account_username,
            ["fetch", owner, slug, str(version), str(target_dir)],
            ok_codes=(0, 3)
        )
        if not out:
            return None
        try:
            parsed = json.loads(out)
            saved = parsed.get("saved", [])
            notes = parsed.get("tried") or []
            if notes:
                logger.info(f"Version {version} output probe for {run_id}: " + "; ".join(notes))
        except Exception:
            saved = []
        if not saved:
            return None
        logger.info(f"Version {version} output for {run_id}: saved {len(saved)} file(s).")
        return target_dir

    @classmethod
    async def download_latest_outputs(
        cls,
        account_username: str,
        kernel_ref: str,
        run_id: str,
        prefer_version: Optional[int] = None,
        probe_previous_for_stop: bool = False
    ):
        """Best-effort output sync. Returns (path, version_used, diagnostics).

        diagnostics is a short list of human-readable strings explaining
        why each versioned attempt produced no files (auth failure,
        non-JSON response, log-only page, etc.) so the pull response can
        surface the real reason when nothing was recovered instead of
        silently falling back to a stub-only log.

        Order of attempts:
          1. prefer_version            - the pinned version holding real output
          2. current version           - for normal runs latest == theirs
             (+ previous version first when probe_previous_for_stop: legacy
              stopped runs have no pin, and the cancelled run sits exactly one
              behind the stop-stub that is now 'current')
          3. plain latest pull         - final fallback
        """
        diagnostics: List[str] = []
        attempts: List[int] = []
        if prefer_version:
            attempts.append(int(prefer_version))
        else:
            current = await cls.get_kernel_current_version(account_username, kernel_ref)
            if not current:
                diagnostics.append("meta: get_kernel_current_version returned None "
                                    "(auth failure, network error, or kernel list unavailable)")
            else:
                if probe_previous_for_stop and current > 1:
                    attempts.append(current - 1)
                attempts.append(current)

        for v in attempts:
            try:
                got = await cls.download_outputs_of_version(account_username, kernel_ref, v, run_id)
            except Exception as e:
                diagnostics.append(f"version {v}: download_outputs_of_version raised: {e}")
                continue
            if got:
                return got, v, diagnostics
            diagnostics.append(
                f"version {v}: no files published (log-only or empty for every "
                f"versionLabel tried - helper notes were logged at INFO level)"
            )

        try:
            plain = await cls.download_outputs(account_username, kernel_ref, run_id)
            return plain, None, diagnostics
        except Exception as e:
            diagnostics.append(f"plain pull failed: {e}")
            return None, None, diagnostics

    @classmethod
    async def download_external_outputs(cls, account_username: str, kernel_ref: str, version: Optional[int] = None):
        """Downloads output for any kernel_ref (not just dashboard runs).

        Uses a synthetic run_id `ext_<account>_<slug>` so files land in
        data/outputs/ext_<account>_<slug>/ and don't pollute the runs table.
        If version is given, fetches that exact version snapshot; if that fails
        (404/403 for old label formats) falls back to plain latest pull which
        is the same output when version == current (common for v1 kernels).
        Returns (path, version_used, diagnostics) like download_latest_outputs.
        """
        owner, _, slug = kernel_ref.partition("/")
        if not owner or not slug:
            return None, None, ["invalid kernel_ref"]
        safe_slug = cls.sanitize_slug(slug)
        safe_owner = "".join(c for c in owner if c.isalnum() or c in ("-", "_")).lower() or "unknown"
        run_id = f"ext_{safe_owner}_{safe_slug}"
        if version:
            # Exact version fetch - try versioned helper first
            got = await cls.download_outputs_of_version(account_username, kernel_ref, int(version), run_id)
            if got:
                return got, int(version), []
            # Fallback: if version-specific failed (e.g. single-version kernels),
            # try plain latest pull ONLY when version is the current/latest version (never for older versions)
            try:
                cur = await cls.get_kernel_current_version(account_username, kernel_ref)
                if cur is None or int(version) == cur:
                    plain, _, diag = await cls.download_latest_outputs(account_username, kernel_ref, run_id)
                    if plain:
                        return plain, int(version), [f"version {version}: versioned probe fell back to latest pull"] + diag
            except Exception:
                pass
            return None, None, [f"version {version}: no files published or recoverable for this specific version snapshot"]
        # For external kernels without a specific version, download latest
        return await cls.download_latest_outputs(account_username, kernel_ref, run_id)

    @classmethod
    async def stop_external_kernel(cls, account_username: str, kernel_ref: str, title: Optional[str] = None) -> Dict[str, Any]:
        """Stops any kernel_ref (even not in DB) by pushing a 1-sec exit stub.

        Throttled via push semaphore like normal pushes. Returns {success, message/error}.
        """
        owner, _, slug = kernel_ref.partition("/")
        if not owner or not slug:
            return {"success": False, "error": "Invalid kernel_ref"}
        # Need title for metadata - use provided or slug
        clean_title = (title or slug)[:50]
        # Use ext-run id for local log file (not DB)
        run_id = f"ext_stop_{owner}_{slug}_{uuid.uuid4().hex[:4]}"
        stop_dir = NOTEBOOKS_DIR / f"stop_{run_id}"
        stop_dir.mkdir(parents=True, exist_ok=True)
        # Always use notebook stub (Kaggle accepts notebook for any kernel type)
        stub_filename = "cell.ipynb"
        stub_nb = {
            "cells": [{
                "cell_type": "code",
                "execution_count": None,
                "metadata": {},
                "outputs": [],
                "source": ["import sys\n", "print('Session explicitly stopped from Account Kernels Explorer.')\n", "sys.exit(0)\n"]
            }],
            "metadata": {"kernelspec": dict(cls.DEFAULT_KERNELSPEC)},
            "nbformat": 4,
            "nbformat_minor": 2
        }
        with open(stop_dir / stub_filename, "w", encoding="utf-8") as f:
            json.dump(stub_nb, f, indent=2)
        metadata = {
            "id": kernel_ref,
            "title": clean_title,
            "code_file": stub_filename,
            "language": "python",
            "kernel_type": "notebook",
            "is_private": "true",
            "enable_gpu": "false",
            "enable_tpu": "false",
            "enable_internet": "false"
        }
        with open(stop_dir / "kernel-metadata.json", "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2)
        cli = get_kaggle_cli_path()
        cmd = [cli, "kernels", "push", "-p", str(stop_dir), "-t", "1"]
        env = AccountManager.get_account_env(account_username)
        tracker.begin(f"ext_stop:{account_username}/{kernel_ref}")
        try:
            async with cls._get_push_semaphore():
                proc = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            out_str = stdout.decode("utf-8", errors="ignore")
            err_str = stderr.decode("utf-8", errors="ignore")
            combined = (out_str + "\n" + err_str).strip()
            if proc.returncode == 0 and "kernel push error" not in combined.lower():
                return {"success": True, "message": f"Stop signal sent to {kernel_ref}"}
            return {"success": False, "error": combined[-500:] or "push failed"}
        except asyncio.TimeoutError:
            return {"success": False, "error": "stop push timed out"}
        except Exception as e:
            return {"success": False, "error": str(e)}
        finally:
            tracker.end(f"ext_stop:{account_username}/{kernel_ref}")

    @classmethod
    async def download_outputs(cls, account_username: str, kernel_ref: str, run_id: str) -> Path:
        """Downloads all output files to `data/outputs/{run_id}`."""
        target_dir = OUTPUTS_DIR / run_id
        target_dir.mkdir(parents=True, exist_ok=True)
        
        cli = get_kaggle_cli_path()
        # NOTE: kernel ref must be the positional argument; `-o` is a boolean
        # force flag in kaggle CLI >= 2.x (not a value-taking option).
        cmd = [cli, "kernels", "output", kernel_ref, "-p", str(target_dir), "-o"]
        env = AccountManager.get_account_env(account_username)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            err_tail = stderr.decode("utf-8", errors="ignore").strip()[-500:]
            raise RuntimeError(f"kaggle kernels output failed (rc={proc.returncode}): {err_tail}")
        return target_dir

    @classmethod
    async def stop_kernel(cls, run_id: str) -> Dict[str, Any]:
        """Stops the active Kaggle run by replacing it with a 1-second exit stub and killing local streams.

        The whole stop pipeline (output pull, version probe, stub push, version
        recovery) is throttled by a global semaphore: "Stop All" fans out over
        every active shard, and unbounded stops spawn dozens of heavyweight CLI
        subprocesses at once - the same OOM that used to kill the server.

        Registers in the ops tracker so the UI keeps the Stop button disabled
        ("Stopping...") even if the page is refreshed mid-stop.
        """
        tracker.begin(run_stop_key(run_id))
        try:
            async with cls._get_stop_semaphore():
                return await cls._stop_kernel_impl(run_id)
        finally:
            tracker.end(run_stop_key(run_id))

    @classmethod
    async def _stop_kernel_impl(cls, run_id: str) -> Dict[str, Any]:
        run = get_run_by_id(run_id)
        if not run:
            return {"success": False, "error": "Run not found"}

        account_username = run["account_username"]
        kernel_slug = run["kernel_slug"]
        kernel_ref = run["kernel_ref"]
        title = run["title"]

        # Kill local follow stream process if active
        if run_id in cls._active_stream_processes:
            try:
                cls._active_stream_processes[run_id].kill()
            except Exception:
                pass
            del cls._active_stream_processes[run_id]

        # CRITICAL: snapshot outputs around stopping. The stop mechanism pushes
        # a new (stub) VERSION, and Kaggle publishes output per VERSION - once
        # the stub lands it is the latest version, so latest-only pulls return
        # just the stub's log. Two saves happen here:
        #   1. BEFORE the stub: whatever is currently published (last completed
        #      version's artifacts) - plain latest pull.
        #   2. AFTER the stub cancels the running version N: Kaggle finalizes
        #      N with its PARTIAL /kaggle/working contents - we fetch exactly
        #      version N via version_label, recovering in-progress shard data.
        try:
            saved_dir = await cls.download_outputs(account_username, kernel_ref, run_id)
            n_saved = sum(1 for p in saved_dir.rglob("*") if p.is_file())
            logger.info(f"Pre-stop output snapshot for {run_id}: {n_saved} file(s) preserved.")
        except Exception as e:
            logger.warning(f"Pre-stop output snapshot failed for {run_id} (continuing): {e}")

        # The version that is current right now is the one the stub will cancel.
        cancelled_version = await cls.get_kernel_current_version(account_username, kernel_ref)

        # Push an immediate-exit stub as a new version to stop the Kaggle worker.
        # Preserve the original kernel type so we don't corrupt the notebook.
        stop_dir = NOTEBOOKS_DIR / f"stop_{run_id}"
        stop_dir.mkdir(parents=True, exist_ok=True)

        is_notebook = (run["code_file"] or "").endswith(".ipynb")
        stub_filename = "cell.ipynb" if is_notebook else "main.py"
        if is_notebook:
            stub_nb = {
                "cells": [{
                    "cell_type": "code",
                    "execution_count": None,
                    "metadata": {},
                    "outputs": [],
                    "source": ["import sys\n", "print('Session explicitly stopped by Kaggle Automation Dashboard.')\n", "sys.exit(0)\n"]
                }],
                "metadata": {"kernelspec": dict(cls.DEFAULT_KERNELSPEC)},
                "nbformat": 4,
                "nbformat_minor": 2
            }
            with open(stop_dir / stub_filename, "w", encoding="utf-8") as f:
                json.dump(stub_nb, f, indent=2)
        else:
            stop_code = "import sys\nprint('Session explicitly stopped by Kaggle Automation Dashboard.')\nsys.exit(0)\n"
            with open(stop_dir / stub_filename, "w", encoding="utf-8") as f:
                f.write(stop_code)

        metadata = {
            "id": kernel_ref,
            "title": title,
            "code_file": stub_filename,
            "language": "python",
            "kernel_type": "notebook" if is_notebook else "script",
            "is_private": "true",
            "enable_gpu": "false",
            "enable_tpu": "false",
            "enable_internet": "false"
        }
        with open(stop_dir / "kernel-metadata.json", "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2)

        cli = get_kaggle_cli_path()
        cmd = [cli, "kernels", "push", "-p", str(stop_dir), "-t", "1"]
        env = AccountManager.get_account_env(account_username)

        # Retry on 409 Conflict (kernel may still be transitioning)
        push_ok = False
        last_err = ""
        for attempt in range(3):
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env
                )
                stdout, stderr = await proc.communicate()
                out_str = stdout.decode("utf-8", errors="ignore")
                err_str = stderr.decode("utf-8", errors="ignore")
                combined = (out_str + "\n" + err_str).strip()
                last_err = combined[-500:]
                is_push_err = "kernel push error" in combined.lower()
                if proc.returncode == 0 and not is_push_err:
                    push_ok = True
                    break
                if ("409" in combined or "conflict" in combined.lower()) and attempt < 2:
                    logger.warning(f"Stop push 409 Conflict, retry {attempt + 1}/3 in 5s...")
                    await asyncio.sleep(5)
                    continue
                break
            except Exception as e:
                logger.warning(f"Stop push completed with warning: {e}")
                last_err = str(e)
                break

        if push_ok:
            update_run_status(run_id, "stopped", "Explicitly stopped by user", utcnow_iso())
            # Sibling runs sharing this kernel died with the same push - stamp
            # them too so reap-grace windows are measured correctly.
            for r in get_active_runs():
                if r["kernel_ref"] == kernel_ref and r["id"] != run_id:
                    update_run_status(
                        r["id"], "stopped",
                        "Stopped: shared kernel received the stop push",
                        utcnow_iso()
                    )

            # Pin the cancelled version as THE output holder for this run so
            # every later manual pull skips the stub and fetches the real
            # partial data - even if local files were deleted in between.
            if cancelled_version:
                set_run_output_version(run_id, cancelled_version)
                for r in get_active_runs():
                    if r["kernel_ref"] == kernel_ref and r["id"] != run_id:
                        set_run_output_version(r["id"], cancelled_version)

                # Recover the CANCELLED version's partial output. Kaggle needs a
                # few seconds to finalize it after the cancel; retry briefly.
                got = None
                for delay in cls.STOP_CAPTURE_RETRY_DELAYS:
                    await asyncio.sleep(delay)
                    got = await cls.download_outputs_of_version(
                        account_username, kernel_ref, cancelled_version, run_id
                    )
                    if got:
                        logger.info(
                            f"Recovered cancelled version {cancelled_version} output "
                            f"of {kernel_ref} into data/outputs/{run_id}"
                        )
                        break
                if not got:
                    logger.info(
                        f"Cancelled version {cancelled_version} of {kernel_ref} had no "
                        f"recoverable output (run may have published nothing yet)."
                    )
                    extra = (f" Cancelled version {cancelled_version} published no output "
                             f"files yet - only its log.")
                else:
                    extra = f" Recovered {len(list(got.glob('*')))} file(s) from cancelled version {cancelled_version}."
            else:
                extra = " (Could not determine the cancelled version number - pull may return the stub log.)"

            return {"success": True,
                    "message": f"Run {run_id} ({kernel_ref}) stopped successfully.{extra}"}

        update_run_status(run_id, run["status"], f"Stop failed: {last_err}")
        return {"success": False, "error": f"Failed to stop run {run_id}: {last_err or 'unknown CLI error'}"}
