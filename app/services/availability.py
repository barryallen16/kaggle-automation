"""GPU slot availability (single source for the quota triangle).

One seam answers "how many GPU slots are free and what runner plan follows":
live slot counting, session-map normalization, runner-plan expansion, and which
kernel VERSIONS still hold a slot. WorkloadDistributor, the runs router (via
AccountManager.active_gpu_runs for DB-only counts), and SessionMonitor
(per-run lifecycle, separate seam) all route through here or explicitly
document why they do not.
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
    from database import (
        get_active_runs,
        get_session_holding_runs,
        update_run_status,
        utcnow_iso,
    )

    from services.kaggle_service import KaggleService

    busy: dict[str, set[str]] = {a: set() for a in accounts}
    if not launch_is_gpu:
        return {a: 0 for a in accounts}

    account_set = set(accounts)

    # Collect candidates first (sync DB scan). Two sources, because a kernel
    # stuck in cancel_acknowledged holds its Kaggle session slot even though
    # the run is finished: get_active_runs() only returns queued/running, so
    # without the second query a torn-down account reads as completely free
    # and every push fails with "Maximum batch GPU session count reached".
    candidates = []
    for r in get_active_runs() + get_session_holding_runs():
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
        # 'cancelling' is non-terminal for SLOTS: Kaggle still counts the
        # session, so it must not be reaped here or the account looks free.
        if st in ("complete", "error", "stopped", "canceled"):
            update_run_status(
                row["id"],
                st,
                "auto-reaped by availability check",
                utcnow_iso(),
            )
            continue
        busy[row["account_username"]].add(row["kernel_ref"])

    return {a: len(busy[a]) for a in accounts}


# Kernel-version statuses that hold a batch GPU session slot. Kaggle counts
# slots per VERSION, not per kernel, so a version left in 'running' keeps a
# slot that no run row owns.
# 'cancel_acknowledged' is deliberately NOT here: it is a teardown state that
# releases on its own. Verified on cassatamaddy 2026-10-17 - six day-old
# cancel_acknowledged versions on one kernel while the account held exactly two
# sessions, one per RUNNING version. Counting them would wedge an account
# permanently, which is the failure this whole seam exists to prevent.
SLOT_HOLDING_VERSION_STATUSES = ("running", "queued", "starting", "pending")


async def held_version_sessions(
    account_username: str, max_versions: int = 5
) -> list[dict[str, Any]]:
    """Kernel versions on an account still burning a GPU session slot.

    The gap this closes: run rows go terminal when a kernel finishes, but a
    stale version can stay in 'running' on Kaggle's side forever (observed: a
    3-day-old v1 holding a slot next to a live v3). Every slot-counting path
    reads run rows, so that slot is invisible - the account reads as half
    free, the push goes out, and Kaggle refuses it with "Maximum batch GPU
    session count of 2 reached" and nothing on screen says why.

    Probes every GPU kernel this dashboard ever pushed on the account (not
    just active ones - the phantom's own run row is long finished). One
    failure path only: never called on the happy path.
    """
    from database import get_gpu_kernel_refs_for_account

    from services.kaggle_outputs import list_kernel_versions

    held: list[dict[str, Any]] = []
    for ref in get_gpu_kernel_refs_for_account(account_username):
        try:
            data = await list_kernel_versions(
                account_username, ref, max_versions
            )
        except Exception as e:
            logger.warning("version probe failed for %s: %s", ref, e)
            continue
        for v in data.get("versions") or []:
            status = str(v.get("status") or "").lower()
            if status in SLOT_HOLDING_VERSION_STATUSES:
                held.append(
                    {
                        "kernel_ref": ref,
                        "version": v.get("version"),
                        "label": v.get("label"),
                        "status": status,
                    }
                )
    return held


def format_held_sessions(held: list[dict[str, Any]]) -> str:
    """Renders held_version_sessions output as an actionable message block.

    Returns "" when nothing is holding a slot, so the caller's own message
    stands unchanged.
    """
    if not held:
        return ""
    lines = "\n".join(
        f"  - {h['kernel_ref']} v{h['version']} ({h['status']})" for h in held
    )
    return (
        f"\nKaggle counts a session per kernel VERSION, so an old version holds a "
        f"slot that no run here owns. {len(held)} version(s) still hold one:\n"
        f"{lines}\n"
        f"Cancel the ones you still need at "
        f"https://www.kaggle.com/code/<slug> (Stop), or free the rest with:\n"
        f"  kaggle kernels delete -y '{held[0]['kernel_ref']}'"
    )
