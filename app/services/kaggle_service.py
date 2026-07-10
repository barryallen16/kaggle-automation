import os
import io
import csv
import json
import uuid
import asyncio
import logging
import re
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional, AsyncGenerator
from app.config import NOTEBOOKS_DIR, LOGS_DIR, OUTPUTS_DIR, get_kaggle_cli_path
from app.services.account_manager import AccountManager
from app.database import create_run_record, update_run_status, get_run_by_id, utcnow_iso

logger = logging.getLogger("kaggle_service")

class KaggleService:
    # Active log stream processes: run_id -> asyncio.subprocess.Process
    _active_stream_processes: Dict[str, asyncio.subprocess.Process] = {}
    # Subscribers for live log broadcasting: run_id -> List[asyncio.Queue]
    _log_subscribers: Dict[str, List[asyncio.Queue]] = {}

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
        workload_id: Optional[str] = None,
        shard_index: Optional[int] = None,
        total_shards: Optional[int] = None
    ) -> Dict[str, Any]:
        """Prepares metadata, writes code, and executes `kaggle kernels push`."""
        run_hash = uuid.uuid4().hex[:6]
        run_id = f"run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{run_hash}"
        # Ensure title fits Kaggle's 50-character limit; derive the slug from
        # exactly what we send as the title. Kaggle keys kernels by the
        # slugified title - a mismatched metadata id makes every re-push 409.
        clean_title = title[:50]
        slug = cls.sanitize_slug(clean_title)
        kernel_ref = f"{account_username}/{slug}"
        kaggle_url = f"https://www.kaggle.com/code/{kernel_ref}"
        
        # Working folder for this run
        run_dir = NOTEBOOKS_DIR / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        
        # Write notebook / script file
        is_notebook = filename.endswith(".ipynb")
        kernel_type = "notebook" if is_notebook else "script"
        if is_notebook:
            code_content = cls.ensure_executable_notebook(code_content)
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

        log_file_path = LOGS_DIR / f"{run_id}.log"
        with open(log_file_path, "w", encoding="utf-8") as f:
            f.write(f"=== Kaggle Run Initialized: {utcnow_iso()} ===\n")
            f.write(f"Kernel: {kernel_ref}\n")
            f.write(f"Title: {title}\n")
            f.write(f"Accelerator: {accelerator}\n")
            f.write(f"Trial Mode: {is_trial} (Timeout: {effective_timeout}s)\n")
            f.write(f"Command: {' '.join(cmd)}\n\n")

        env = AccountManager.get_account_env(account_username)
        
        # Retry logic for 409 Conflict errors (Kaggle rate-limits when a kernel
        # is still starting/running on the same account)
        MAX_RETRIES = 4
        RETRY_BASE_DELAY = 5  # seconds
        
        out_str = ""
        err_str = ""
        proc = None
        
        try:
            for attempt in range(MAX_RETRIES):
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env
                )
                stdout, stderr = await proc.communicate()
                out_str = stdout.decode("utf-8", errors="ignore")
                err_str = stderr.decode("utf-8", errors="ignore")

                # Check for 409 Conflict — retry with exponential backoff
                if proc.returncode != 0 and "409" in err_str and attempt < MAX_RETRIES - 1:
                    delay = RETRY_BASE_DELAY * (2 ** attempt)
                    logger.warning(f"Kaggle 409 Conflict on attempt {attempt + 1}/{MAX_RETRIES}, retrying in {delay}s...")
                    with open(log_file_path, "a", encoding="utf-8") as f:
                        f.write(f"[RETRY] 409 Conflict on attempt {attempt + 1}, retrying in {delay}s...\n")
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
            if not is_error:
                asyncio.create_task(cls.start_background_log_stream(run_id, account_username, kernel_ref, log_file_path))

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

    @classmethod
    async def get_kernel_status(cls, account_username: str, kernel_ref: str) -> Dict[str, Any]:
        """Queries `kaggle kernels status <kernel_ref>`."""
        cli = get_kaggle_cli_path()
        cmd = [cli, "kernels", "status", kernel_ref]
        env = AccountManager.get_account_env(account_username)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env
            )
            stdout, stderr = await proc.communicate()
            out_str = stdout.decode("utf-8", errors="ignore").strip()
            
            # Status parsing: e.g. 'username/slug has status "running"' or 'complete' or 'error'
            status_match = re.search(r'status "(.*?)"', out_str, re.IGNORECASE)
            status = status_match.group(1).lower() if status_match else "unknown"
            
            if "complete" in out_str.lower():
                status = "complete"
            elif "running" in out_str.lower():
                status = "running"
            elif "queued" in out_str.lower():
                status = "queued"
            elif "error" in out_str.lower():
                status = "error"
            elif "cancelAck" in out_str.lower() or "canceled" in out_str.lower():
                status = "stopped"

            return {
                "success": True,
                "raw": out_str,
                "status": status
            }
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
        """Stops the active Kaggle run by replacing it with a 1-second exit stub and killing local streams."""
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
                _, stderr = await proc.communicate()
                err_str = stderr.decode("utf-8", errors="ignore")
                last_err = err_str.strip()[-500:]
                if proc.returncode == 0:
                    push_ok = True
                    break
                if "409" in err_str and attempt < 2:
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
            return {"success": True, "message": f"Run {run_id} ({kernel_ref}) stopped successfully."}

        update_run_status(run_id, run["status"], f"Stop failed: {last_err}")
        return {"success": False, "error": f"Failed to stop run {run_id}: {last_err or 'unknown CLI error'}"}
