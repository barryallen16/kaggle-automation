import os
import io
import csv
import json
import uuid
import asyncio
import logging
import re
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Any, Optional, AsyncGenerator
from app.config import NOTEBOOKS_DIR, LOGS_DIR, OUTPUTS_DIR
from app.services.account_manager import AccountManager
from app.database import create_run_record, update_run_status, get_run_by_id

logger = logging.getLogger("kaggle_service")

class KaggleService:
    # Active log stream processes: run_id -> asyncio.subprocess.Process
    _active_stream_processes: Dict[str, asyncio.subprocess.Process] = {}
    # Subscribers for live log broadcasting: run_id -> List[asyncio.Queue]
    _log_subscribers: Dict[str, List[asyncio.Queue]] = {}

    @classmethod
    def sanitize_slug(cls, title: str) -> str:
        slug = re.sub(r"[^a-zA-Z0-9\-]", "-", title.lower()).strip("-")
        slug = re.sub(r"-+", "-", slug)
        return slug or f"nb-{uuid.uuid4().hex[:6]}"

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
        run_id = f"run_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        slug = cls.sanitize_slug(title)
        kernel_ref = f"{account_username}/{slug}"
        kaggle_url = f"https://www.kaggle.com/code/{kernel_ref}"
        
        # Working folder for this run
        run_dir = NOTEBOOKS_DIR / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        
        # Write notebook / script file
        code_path = run_dir / filename
        with open(code_path, "w", encoding="utf-8") as f:
            f.write(code_content)
        
        # Accelerator flags
        acc_lower = accelerator.lower()
        enable_gpu = "true" if ("gpu" in acc_lower or "t4" in acc_lower) else "false"
        enable_tpu = "true" if "tpu" in acc_lower else "false"
        
        is_notebook = filename.endswith(".ipynb")
        kernel_type = "notebook" if is_notebook else "script"
        
        metadata = {
            "id": kernel_ref,
            "title": title,
            "code_file": filename,
            "language": "python",
            "kernel_type": kernel_type,
            "is_private": "true",
            "enable_gpu": enable_gpu,
            "enable_tpu": enable_tpu,
            "enable_internet": "true" if enable_internet else "false",
            "dataset_sources": [],
            "competition_sources": [],
            "kernel_sources": [],
            "model_sources": []
        }
        
        meta_path = run_dir / "kernel-metadata.json"
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2)

        # Build CLI command
        cmd = ["kaggle", "kernels", "push", "-p", str(run_dir)]
        
        # Accelerator CLI option
        if accelerator and accelerator != "none" and accelerator != "default":
            cmd.extend(["--accelerator", accelerator])
        
        # Timeout handling (for trial run or custom timeout)
        effective_timeout = timeout_seconds or (300 if is_trial else 43200)
        if effective_timeout:
            cmd.extend(["-t", str(effective_timeout)])

        log_file_path = LOGS_DIR / f"{run_id}.log"
        with open(log_file_path, "w", encoding="utf-8") as f:
            f.write(f"=== Kaggle Run Initialized: {datetime.utcnow().isoformat()} ===\n")
            f.write(f"Kernel: {kernel_ref}\n")
            f.write(f"Title: {title}\n")
            f.write(f"Accelerator: {accelerator}\n")
            f.write(f"Trial Mode: {is_trial} (Timeout: {effective_timeout}s)\n")
            f.write(f"Command: {' '.join(cmd)}\n\n")

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
            err_str = stderr.decode("utf-8", errors="ignore")

            with open(log_file_path, "a", encoding="utf-8") as f:
                if out_str:
                    f.write(f"[OUTPUT]\n{out_str}\n")
                if err_str:
                    f.write(f"[STDERR]\n{err_str}\n")

            is_error = proc.returncode != 0 or "Kernel push error" in out_str or "Error" in err_str
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
                "start_time": datetime.utcnow().isoformat(),
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
        cmd = ["kaggle", "kernels", "status", kernel_ref]
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
        cmd = ["kaggle", "kernels", "logs", kernel_ref]
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
        """Streams live logs via `kaggle kernels logs -f <kernel_ref>` to file and WebSocket subscribers."""
        cmd = ["kaggle", "kernels", "logs", "-f", "--interval", "3", kernel_ref]
        env = AccountManager.get_account_env(account_username)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env
            )
            cls._active_stream_processes[run_id] = proc

            with open(log_file, "a", encoding="utf-8") as f:
                f.write(f"\n--- Live Stream Connected [{datetime.utcnow().isoformat()}] ---\n")

            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                decoded = line.decode("utf-8", errors="ignore")
                
                # Append to local log file
                with open(log_file, "a", encoding="utf-8") as f:
                    f.write(decoded)
                    f.flush()

                # Broadcast to active WebSocket queues
                if run_id in cls._log_subscribers:
                    for q in list(cls._log_subscribers[run_id]):
                        await q.put(decoded)

            await proc.wait()
        except Exception as e:
            logger.error(f"Live log streaming ended with error for {run_id}: {e}")
        finally:
            if run_id in cls._active_stream_processes:
                del cls._active_stream_processes[run_id]

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
        cmd = ["kaggle", "kernels", "files", "-v", kernel_ref]
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
        
        cmd = ["kaggle", "kernels", "output", "-p", str(target_dir), "-o", kernel_ref]
        env = AccountManager.get_account_env(account_username)
        
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env
        )
        stdout, stderr = await proc.communicate()
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

        # Push immediate exit stub to stop Kaggle execution worker
        stop_dir = NOTEBOOKS_DIR / f"stop_{run_id}"
        stop_dir.mkdir(parents=True, exist_ok=True)

        stop_code = "import sys\nprint('Session explicitly stopped by Kaggle Automation Dashboard.')\nsys.exit(0)\n"
        with open(stop_dir / "main.py", "w", encoding="utf-8") as f:
            f.write(stop_code)

        metadata = {
            "id": kernel_ref,
            "title": title,
            "code_file": "main.py",
            "language": "python",
            "kernel_type": "script",
            "is_private": "true",
            "enable_gpu": "false",
            "enable_tpu": "false",
            "enable_internet": "false"
        }
        with open(stop_dir / "kernel-metadata.json", "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2)

        cmd = ["kaggle", "kernels", "push", "-p", str(stop_dir), "-t", "1"]
        env = AccountManager.get_account_env(account_username)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env
            )
            await proc.communicate()
        except Exception as e:
            logger.warning(f"Stop push completed with warning: {e}")

        update_run_status(run_id, "stopped", "Explicitly stopped by user", datetime.utcnow().isoformat())
        return {"success": True, "message": f"Run {run_id} ({kernel_ref}) stopped successfully."}
