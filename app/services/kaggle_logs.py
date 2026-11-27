import asyncio
import logging
from pathlib import Path
from typing import Any

from config import get_kaggle_cli_path
from database import utcnow_iso

from services.account_manager import AccountManager
from services.kaggle_kernel_identity import (
    TERMINAL_KERNEL_STATUSES,
    strip_kaggle_cli_warnings,
)
from services.kaggle_status import get_kernel_status

logger = logging.getLogger("kaggle_logs")

_active_stream_processes: dict[str, asyncio.subprocess.Process] = {}
_active_stream_tasks: dict[str, asyncio.Task] = {}
_log_subscribers: dict[str, list[asyncio.Queue]] = {}


async def start_background_log_stream(
    run_id: str, account_username: str, kernel_ref: str, log_file: Path
):
    task = asyncio.current_task()
    if task is not None:
        _active_stream_tasks[run_id] = task
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
        for q in list(_log_subscribers.get(run_id, [])):
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
                    env=env,
                    limit=8 * 1024 * 1024,
                )
                _active_stream_processes[run_id] = proc
                drainer = asyncio.create_task(drain_stderr(proc.stderr))

                if first_attach:
                    append_and_broadcast(
                        f"\n--- Live Stream Connected [{utcnow_iso()}] ---\n"
                    )
                    first_attach = False
                else:
                    append_and_broadcast(
                        f"\n--- Live Stream Re-attached [{utcnow_iso()}] ---\n"
                    )

                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        break
                    text = strip_kaggle_cli_warnings(
                        line.decode("utf-8", errors="ignore")
                    )
                    if not text:
                        continue
                    produced = True
                    append_and_broadcast(text)
                rc = await proc.wait()
            finally:
                if drainer:
                    drainer.cancel()
                if _active_stream_processes.get(run_id) is proc:
                    del _active_stream_processes[run_id]
                if proc and proc.returncode is None:
                    try:
                        proc.kill()
                    except Exception:
                        pass

            status_resp = await get_kernel_status(account_username, kernel_ref)
            status = status_resp.get("status", "unknown")
            if status in TERMINAL_KERNEL_STATUSES:
                append_and_broadcast(
                    f"\n--- Stream ended: kernel {status} [{utcnow_iso()}] ---\n"
                )
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
        _active_stream_processes.pop(run_id, None)
        if task is None or _active_stream_tasks.get(run_id) is task:
            _active_stream_tasks.pop(run_id, None)


def stop_log_stream(run_id: str) -> None:
    task = _active_stream_tasks.pop(run_id, None)
    if task is not None and not task.done():
        task.cancel()
    proc = _active_stream_processes.pop(run_id, None)
    if proc is not None and getattr(proc, "returncode", None) is None:
        try:
            proc.kill()
        except Exception:
            pass


def ensure_log_stream(run: dict[str, Any]) -> None:
    run_id = run.get("id")
    if not run_id:
        return
    existing_task = _active_stream_tasks.get(run_id)
    if existing_task is not None and not existing_task.done():
        return
    existing = _active_stream_processes.get(run_id)
    if existing is not None and existing.returncode is None:
        return
    if run.get("status") not in ("queued", "running"):
        return
    log_file = run.get("log_file")
    if not log_file:
        return
    task = asyncio.create_task(
        start_background_log_stream(
            run_id, run["account_username"], run["kernel_ref"], Path(log_file)
        )
    )
    _active_stream_tasks[run_id] = task


def register_log_subscriber(run_id: str) -> asyncio.Queue:
    queue: asyncio.Queue = asyncio.Queue()
    if run_id not in _log_subscribers:
        _log_subscribers[run_id] = []
    _log_subscribers[run_id].append(queue)
    return queue


def unregister_log_subscriber(run_id: str, queue: asyncio.Queue):
    if run_id in _log_subscribers and queue in _log_subscribers[run_id]:
        _log_subscribers[run_id].remove(queue)
        if not _log_subscribers[run_id]:
            del _log_subscribers[run_id]
