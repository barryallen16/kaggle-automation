import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, Any
from app.config import MAX_KAGGLE_SESSION_SECONDS, WARNING_BEFORE_EXPIRY_SECONDS
from app.database import (
    get_active_runs, update_run_status, update_run_telegram_flag, get_run_by_id
)
from app.services.kaggle_service import KaggleService
from app.services.telegram_service import TelegramService

logger = logging.getLogger("session_monitor")

class SessionMonitor:
    _is_running: bool = False
    _monitor_task: asyncio.Task = None

    @staticmethod
    def _parse_start(value: str) -> datetime:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)  # legacy rows were stored as naive UTC
        return dt

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

        now = datetime.now(timezone.utc)

        for run in active_runs:
            # Isolate failures: one bad run must never stall the others in this cycle
            try:
                await cls._check_single_run(run, now)
            except Exception as e:
                logger.error(f"Error checking run {run.get('id')}: {e}")

    @classmethod
    async def _check_single_run(cls, run: Dict[str, Any], now: datetime):
        run_id = run["id"]
        account_username = run["account_username"]
        kernel_ref = run["kernel_ref"]

        try:
            start_dt = cls._parse_start(run["start_time"])
            elapsed_seconds = (now - start_dt).total_seconds()
        except Exception:
            elapsed_seconds = 0

        # 1. Check Kaggle kernel status via CLI
        status_resp = await KaggleService.get_kernel_status(account_username, kernel_ref)
        remote_status = status_resp.get("status", "unknown")

        is_trial = bool(run.get("is_trial"))

        # 2. Check if run started and notify Telegram
        if (remote_status == "running" or elapsed_seconds > 60) and run.get("telegram_notified_start") == 0:
            await TelegramService.notify_run_started(run)
            update_run_telegram_flag(run_id, "telegram_notified_start", 1)

        # 3/4. Long-session alerts only apply to full 12h runs (never to short trials)
        if not is_trial:
            # 11-Hour Warning (1 hour before 12-hour limit)
            if elapsed_seconds >= (MAX_KAGGLE_SESSION_SECONDS - WARNING_BEFORE_EXPIRY_SECONDS) and run.get("telegram_notified_11h") == 0:
                await TelegramService.notify_11h_warning(run)
                update_run_telegram_flag(run_id, "telegram_notified_11h", 1)

            # 12-Hour Expiry Alert
            if elapsed_seconds >= MAX_KAGGLE_SESSION_SECONDS and run.get("telegram_notified_12h") == 0:
                await TelegramService.notify_12h_limit_reached(run)
                update_run_telegram_flag(run_id, "telegram_notified_12h", 1)

        # 5. Handle completion, error, or stop
        if remote_status in ["complete", "error", "stopped", "canceled"]:
            update_run_status(
                run_id=run_id,
                status="stopped" if remote_status in ("stopped", "canceled") else remote_status,
                status_message=status_resp.get("raw", ""),
                end_time=now.isoformat()
            )
            if run.get("telegram_notified_end") == 0:
                await TelegramService.notify_run_completed(run, remote_status)
                update_run_telegram_flag(run_id, "telegram_notified_end", 1)
        elif remote_status != "unknown" and remote_status != run["status"]:
            update_run_status(run_id=run_id, status=remote_status)
