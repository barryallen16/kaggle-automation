"""GPU slot availability (single source for the quota triangle).

One seam answers "how many GPU slots are free and what runner plan follows":
live slot counting, session-map normalization, and runner-plan expansion.
WorkloadDistributor, the runs router (via AccountManager.active_gpu_runs for
DB-only counts), and SessionMonitor (per-run lifecycle, separate seam) all
route through here or explicitly document why they do not.
"""

import asyncio
import logging
import os
from typing import Any

from config import KAGGLE_MAX_GPU_SESSIONS_PER_ACCOUNT, is_gpu_accelerator

logger = logging.getLogger("availability")

STATUS_CHECK_CONCURRENCY = max(1, int(os.getenv("DISTRIBUTED_STATUS_CONCURRENCY", "3")))


def free_slots(chosen: int, busy: int, gpu_launch: bool) -> int:
    """Effective runners for one account: min(chosen, free), uncapped for CPU."""
    if not gpu_launch:
        return chosen
    return min(chosen, max(0, KAGGLE_MAX_GPU_SESSIONS_PER_ACCOUNT - busy))


def normalize_sessions_map(
    sessions_per_account: int | dict[str, int] | None, accounts: list[str]
) -> dict[str, int]:
    """Accepts a global count OR per-account overrides {username: 1|2}.

    Per-account values are clamped to Kaggle's 1..2 batch-GPU range;
    accounts missing from the map fall back to the global default of 2.
    """
    if isinstance(sessions_per_account, dict):
        default = max(1, min(KAGGLE_MAX_GPU_SESSIONS_PER_ACCOUNT, 2))
        out = {a: default for a in accounts}
        for acc, val in sessions_per_account.items():
            if acc in out:
                try:
                    out[acc] = max(
                        1, min(KAGGLE_MAX_GPU_SESSIONS_PER_ACCOUNT, int(val))
                    )
                except (TypeError, ValueError):
                    continue
        return out
    try:
        n = int(sessions_per_account if sessions_per_account is not None else 2)
    except (TypeError, ValueError):
        n = 2
    n = max(1, min(KAGGLE_MAX_GPU_SESSIONS_PER_ACCOUNT, n))
    return {a: n for a in accounts}


def build_runner_plan(
    accounts: list[str],
    sessions_map: dict[str, int],
    accelerator: str,
    busy: dict[str, int],
) -> list[dict[str, Any]]:
    """Expands accounts into runners based on free slots (silent reduction).

    Account capacity is Kaggle's hard limit of 2 concurrent GPU sessions:
    free = max(0, 2 - busy); effective = min(chosen, free). CPU launches
    aren't capped by that limit at all. `chosen` comes from the per-account
    sessions map (which may itself be a uniform global value).
    """
    gpu_launch = is_gpu_accelerator(accelerator)
    plan = []
    for a in accounts:
        chosen = sessions_map.get(a, 2)
        b = busy.get(a, 0)
        plan.append(
            {
                "account": a,
                "requested": chosen,
                "busy": b,
                "slots": free_slots(chosen, b, gpu_launch),
            }
        )
    return plan


async def live_busy_sessions(
    accounts: list[str], launch_is_gpu: bool
) -> dict[str, int]:
    """Counts busy GPU session slots per account (live Kaggle status).

    Queries live Kaggle status for active (queued/running) GPU runs in the DB.
    If a kernel is complete, error, or stopped, its DB record is reaped
    and its slot freed immediately. Genuinely active (running/queued)
    kernels consume 1 slot per distinct kernel_ref.

    Throttled to STATUS_CHECK_CONCURRENCY parallel `kaggle kernels status`
    calls so 16 accounts with 30+ active runs don't spawn 30 CLI processes
    at once (the same OOM that kills pushes).
    """
    from database import get_active_runs, update_run_status, utcnow_iso

    from services.kaggle_service import KaggleService

    busy: dict[str, set[str]] = {a: set() for a in accounts}
    if not launch_is_gpu:
        return {a: 0 for a in accounts}

    account_set = set(accounts)

    # Collect candidates first (sync DB scan)
    candidates = []
    for r in get_active_runs():
        acc = r.get("account_username")
        if acc not in account_set:
            continue
        if not is_gpu_accelerator(r.get("accelerator")):
            continue
        candidates.append(r)

    if not candidates:
        return {a: 0 for a in accounts}

    sem = asyncio.Semaphore(STATUS_CHECK_CONCURRENCY)

    async def check_one(row):
        async with sem:
            resp = await KaggleService.get_kernel_status(
                row["account_username"], row["kernel_ref"]
            )
        return row, resp.get("status", "unknown")

    results = await asyncio.gather(*[check_one(r) for r in candidates])

    for row, st in results:
        if st in ("complete", "error", "stopped", "cancelacknowledged"):
            update_run_status(
                row["id"],
                "stopped" if "cancel" in st else st,
                "auto-reaped by availability check",
                utcnow_iso(),
            )
            continue
        busy[row["account_username"]].add(row["kernel_ref"])

    return {a: len(busy[a]) for a in accounts}
