"""Live log-stream hub (extracted from KaggleService).

Owns the follower-process registry and subscriber queues: one seam for
starting, ensuring, and broadcasting live kernel logs. Status checks go
through kaggle_status directly; the KaggleService facade keeps aliases so
existing callers and test patches keep working.
"""

import asyncio
import logging
from pathlib import Path
from typing import Any

from config import get_kaggle_cli_path
from database import utcnow_iso

from services.account_manager import AccountManager
from services.kaggle_status import get_kernel_status

logger = logging.getLogger("kaggle_logs")

# Active log stream processes: run_id -> asyncio.subprocess.Process
_active_stream_processes: dict[str, asyncio.subprocess.Process] = {}
# Subscribers for live log broadcasting: run_id -> List[asyncio.Queue]
_log_subscribers: dict[str, list[asyncio.Queue]] = {}


async def start_background_log_stream(
    run_id: str, account_username: str, kernel_ref: str, log_file: Path
):
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
                    # 64KB default kills the follower on long tqdm bars.
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
                    produced = True
                    append_and_broadcast(line.decode("utf-8", errors="ignore"))
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

            # Terminal kernel state? Nothing more will ever arrive.
            status_resp = await get_kernel_status(account_username, kernel_ref)
            status = status_resp.get("status", "unknown")
            if status in ("complete", "error", "stopped"):
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


def ensure_log_stream(run: dict[str, Any]) -> None:
    """(Re)starts the background log follower for an active run if none is alive.

    Self-healing for streamers lost to server restarts or crashes: opening
    the Logs view (or the WebSocket) on an active run revives the producer
    without re-pushing anything.
    """
    run_id = run.get("id")
    if not run_id:
        return
    existing = _active_stream_processes.get(run_id)
    if existing is not None and existing.returncode is None:
        return  # a follower is already alive
    if run.get("status") not in ("queued", "running"):
        return  # finished runs have no live output
    log_file = run.get("log_file")
    if not log_file:
        return
    asyncio.create_task(
        start_background_log_stream(
            run_id, run["account_username"], run["kernel_ref"], Path(log_file)
        )
    )


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
