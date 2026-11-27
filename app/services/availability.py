import asyncio
import logging
import os
from typing import Any

from config import KAGGLE_MAX_GPU_SESSIONS_PER_ACCOUNT, is_gpu_accelerator

logger = logging.getLogger("availability")

STATUS_CHECK_CONCURRENCY = max(1, int(os.getenv("DISTRIBUTED_STATUS_CONCURRENCY", "3")))


def free_slots(chosen: int, busy: int, gpu_launch: bool) -> int:
    if not gpu_launch:
        return chosen
    return min(chosen, max(0, KAGGLE_MAX_GPU_SESSIONS_PER_ACCOUNT - busy))


def normalize_sessions_map(
    sessions_per_account: int | dict[str, int] | None, accounts: list[str]
) -> dict[str, int]:
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


SLOT_HOLDING_VERSION_STATUSES = ("running", "queued", "starting", "pending")


async def held_version_sessions(
    account_username: str, max_versions: int = 5
) -> list[dict[str, Any]]:
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
