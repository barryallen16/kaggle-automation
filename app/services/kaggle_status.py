"""Kernel status + log fetching (leaf module extracted from KaggleService).

Small interface: get_kernel_status, fetch_full_logs. Throttling still goes
through KaggleService's semaphore (test-pinned seam, see
tests/test_username_autocorrect.py) via a lazy import to avoid a cycle.
"""

import asyncio
import logging
import os
import re
from typing import Any

from config import get_kaggle_cli_path

from services.account_manager import AccountManager
from services.kaggle_kernel_identity import normalize_kernel_status

logger = logging.getLogger("kaggle_status")

KERNEL_STATUS_TIMEOUT_SECONDS = int(os.getenv("KERNEL_STATUS_TIMEOUT_SECONDS", "90"))


async def get_kernel_status(account_username: str, kernel_ref: str) -> dict[str, Any]:
    """Queries `kaggle kernels status <kernel_ref>` - throttled + bounded."""
    from services.kaggle_service import KaggleService  # lazy: test-pinned seam

    cli = get_kaggle_cli_path()
    cmd = [cli, "kernels", "status", kernel_ref]
    env = AccountManager.get_account_env(account_username)

    proc = None
    try:
        async with KaggleService._get_kernel_status_semaphore():
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                ),
                timeout=KERNEL_STATUS_TIMEOUT_SECONDS,
            )
            stdout, _stderr = await asyncio.wait_for(
                proc.communicate(), timeout=KERNEL_STATUS_TIMEOUT_SECONDS
            )
        out_str = stdout.decode("utf-8", errors="ignore").strip()

        # Status parsing: e.g. 'username/slug has status "running"'
        status_match = re.search(r'status "(.*?)"', out_str, re.IGNORECASE)
        captured = status_match.group(1) if status_match else out_str
        status = normalize_kernel_status(captured)

        return {"success": True, "raw": out_str, "status": status}
    except TimeoutError:
        logger.warning(
            f"get_kernel_status timed out for {kernel_ref} (@{account_username})"
        )
        return {"success": False, "status": "unknown", "error": "timeout"}
    except Exception as e:
        return {"success": False, "status": "unknown", "error": str(e)}
    finally:
        if proc is not None and getattr(proc, "returncode", None) is None:
            if hasattr(proc, "kill"):
                try:
                    proc.kill()
                except Exception:
                    pass
            if hasattr(proc, "wait"):
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except Exception:
                    pass


async def fetch_full_logs(account_username: str, kernel_ref: str) -> str:
    """Fetches the latest execution logs using `kaggle kernels logs <kernel_ref>`."""
    from services.kaggle_service import KaggleService

    cli = get_kaggle_cli_path()
    cmd = [cli, "kernels", "logs", kernel_ref]
    env = AccountManager.get_account_env(account_username)
    proc = None
    try:
        async with KaggleService._get_kernel_status_semaphore():
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=KERNEL_STATUS_TIMEOUT_SECONDS
            )
        logs = stdout.decode("utf-8", errors="ignore")
        err = stderr.decode("utf-8", errors="ignore")
        return logs if logs else err
    except TimeoutError:
        return f"Error fetching logs: timed out after {KERNEL_STATUS_TIMEOUT_SECONDS}s"
    except Exception as e:
        return f"Error fetching logs: {e!s}"
    finally:
        if proc is not None and getattr(proc, "returncode", None) is None:
            if hasattr(proc, "kill"):
                try:
                    proc.kill()
                except Exception:
                    pass
            if hasattr(proc, "wait"):
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except Exception:
                    pass
