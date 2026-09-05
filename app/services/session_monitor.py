import asyncio
import logging
import os
import time
from datetime import UTC, datetime
from typing import Any, ClassVar

from config import MAX_KAGGLE_SESSION_SECONDS, WARNING_BEFORE_EXPIRY_SECONDS

# Throttle parallel status checks - 32 runs checking status at once is the same
# OOM spike as pushes (each `kaggle kernels status` is a CLI process).
MONITOR_CONCURRENCY = max(1, int(os.getenv("MONITOR_CONCURRENCY", "3")))
from database import (
    get_active_runs,
    set_run_output_version,
    update_run_status,
    update_run_telegram_flag,
)

from services.account_manager import AccountManager
from services.kaggle_service import KaggleService
from services.telegram_service import TelegramService

logger = logging.getLogger("session_monitor")


class SessionMonitor:
    _is_running: bool = False
    _monitor_task: asyncio.Task = None

    # Live-quota lookups are expensive CLI calls - cache per account so a busy
    # dashboard with many shards queries each account at most once per TTL.
    QUOTA_CACHE_TTL_SECONDS = 300
    _quota_cache: ClassVar[dict[str, tuple[float, dict[str, Any]]]] = {}

    @staticmethod
    def _parse_start(value: str) -> datetime:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)  # legacy rows were stored as naive UTC
        return dt

    @staticmethod
    def _is_gpu_accelerator(accelerator: Any) -> bool:
        acc = str(accelerator or "").lower()
        return bool(acc) and acc not in ("none", "default", "cpu")

    @classmethod
    async def _gpu_quota_exhausted(cls, account_username: str) -> bool:
        """True only when Kaggle reports the account's weekly GPU quota FULLY spent.

        Unknown/unparsable quota data returns False - we never force-stop a run
        based on missing information.
        """
        now = time.monotonic()
        cached = cls._quota_cache.get(account_username)
        if cached and (now - cached[0]) < cls.QUOTA_CACHE_TTL_SECONDS:
            quota = cached[1]
        else:
            try:
                quota = await AccountManager.refresh_account_quota(account_username)
            except Exception as e:
                logger.warning(f"Quota refresh failed for @{account_username}: {e}")
                quota = {}
            if not quota:
                return False
            cls._quota_cache[account_username] = (now, quota)

        gpu = quota.get("gpu") or {}
        used, limit = gpu.get("used"), gpu.get("limit")
        try:
            if used is None or not limit:
                return False
            return float(used) >= float(limit)
        except (TypeError, ValueError):
            return False

    @classmethod
    async def start(cls):
        if cls._is_running:
            return
        cls._is_running = True
        cls._monitor_task = asyncio.create_task(cls._monitor_loop())
        logger.info("Background Kaggle Session Monitor started.")

    @classmethod
    async def stop(cls):
        cls._is_running = False
        if cls._monitor_task:
            cls._monitor_task.cancel()
            try:
                await cls._monitor_task
            except asyncio.CancelledError:
                pass
        logger.info("Background Kaggle Session Monitor stopped.")

    @classmethod
    async def _monitor_loop(cls):
        while cls._is_running:
            try:
                await cls.check_active_sessions()
            except Exception as e:
                logger.error(f"Error in session monitor loop: {e}")
            await asyncio.sleep(30)

    @classmethod
    async def check_active_sessions(cls):
        active_runs = get_active_runs()
        if not active_runs:
            return

        now = datetime.now(UTC)
        sem = asyncio.Semaphore(MONITOR_CONCURRENCY)

        async def guarded(run):
            async with sem:
                try:
                    await cls._check_single_run(run, now)
                except Exception as e:
                    logger.error(f"Error checking run {run.get('id')}: {e}")

        await asyncio.gather(*[guarded(r) for r in active_runs])

    @classmethod
    async def _check_single_run(cls, run: dict[str, Any], now: datetime):
        run_id = run["id"]
        account_username = run["account_username"]
        kernel_ref = run["kernel_ref"]

        try:
            start_dt = cls._parse_start(run["start_time"])
            elapsed_seconds = (now - start_dt).total_seconds()
        except Exception:
            elapsed_seconds = 0

        # 1. Check Kaggle kernel status via CLI
        status_resp = await KaggleService.get_kernel_status(
            account_username, kernel_ref
        )
        remote_status = status_resp.get("status", "unknown")

        is_trial = bool(run.get("is_trial"))

        # 2. Check if run started and notify Telegram
        if (remote_status == "running" or elapsed_seconds > 60) and run.get(
            "telegram_notified_start"
        ) == 0:
            await TelegramService.notify_run_started(run)
            update_run_telegram_flag(run_id, "telegram_notified_start", 1)

        # 3/4. Long-session alerts only apply to full 12h runs (never to short trials)
        if not is_trial:
            # 11-Hour Warning (1 hour before 12-hour limit)
            if (
                elapsed_seconds
                >= (MAX_KAGGLE_SESSION_SECONDS - WARNING_BEFORE_EXPIRY_SECONDS)
                and run.get("telegram_notified_11h") == 0
            ):
                await TelegramService.notify_11h_warning(run)
                update_run_telegram_flag(run_id, "telegram_notified_11h", 1)

            # 12-Hour Expiry Alert
            if (
                elapsed_seconds >= MAX_KAGGLE_SESSION_SECONDS
                and run.get("telegram_notified_12h") == 0
            ):
                await TelegramService.notify_12h_limit_reached(run)
                update_run_telegram_flag(run_id, "telegram_notified_12h", 1)

        # 5. Quota-exhaustion guard: Kaggle keeps reporting the kernel as
        #    "running" even after the account's weekly GPU quota is fully
        #    spent (nothing progresses, no completion ever arrives). A run
        #    must end on script completion, the 12h limit - or quota gone.
        terminal = ("complete", "error", "stopped", "canceled")
        if (
            remote_status not in terminal
            and cls._is_gpu_accelerator(run.get("accelerator"))
            and await cls._gpu_quota_exhausted(account_username)
        ):
            logger.warning(
                f"GPU quota exhausted for @{account_username}; force-stopping run {run_id}"
            )
            stop_resp = await KaggleService.stop_kernel(run_id)
            update_run_status(
                run_id=run_id,
                status="stopped",
                status_message="Stopped by monitor: weekly GPU quota fully exhausted",
                end_time=now.isoformat(),
            )
            if run.get("telegram_notified_end") == 0:
                await TelegramService.notify_run_completed(
                    run, "stopped (GPU quota exhausted)"
                )
                update_run_telegram_flag(run_id, "telegram_notified_end", 1)
            if not stop_resp.get("success"):
                logger.warning(
                    f"Force-stop push failed for {run_id}: {stop_resp.get('error')}"
                )
            return

        # 6. Handle completion, error, or stop
        if remote_status in ["complete", "error", "stopped", "canceled"]:
            update_run_status(
                run_id=run_id,
                status="stopped"
                if remote_status in ("stopped", "canceled")
                else remote_status,
                status_message=status_resp.get("raw", ""),
                end_time=now.isoformat(),
            )
            # Auto-sync output artifacts the moment a run ends, so the Files
            # tab shows the real files without a manual "Pull from Kaggle".
            # Version-aware: pulls THIS run's exact version snapshot (which is
            # what Kaggle finalizes on completion/error/cancel) and pins it on
            # the run row so later manual pulls keep working after deletions.
            try:
                (
                    _got_path,
                    used_version,
                    _diag,
                ) = await KaggleService.download_latest_outputs(
                    account_username,
                    kernel_ref,
                    run_id,
                    prefer_version=run.get("output_version"),
                )
                if used_version and not run.get("output_version"):
                    set_run_output_version(run_id, used_version)
            except Exception as e:
                logger.info(f"Auto-pull of outputs skipped for {run_id}: {e}")
            if run.get("telegram_notified_end") == 0:
                await TelegramService.notify_run_completed(run, remote_status)
                update_run_telegram_flag(run_id, "telegram_notified_end", 1)
        elif remote_status != "unknown" and remote_status != run["status"]:
            update_run_status(run_id=run_id, status=remote_status)
