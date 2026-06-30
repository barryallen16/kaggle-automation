import asyncio
import logging
from datetime import datetime
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

        now = datetime.utcnow()

        for run in active_runs:
            run_id = run["id"]
            account_username = run["account_username"]
            kernel_ref = run["kernel_ref"]
            
            try:
                start_dt = datetime.fromisoformat(run["start_time"])
                elapsed_seconds = (now - start_dt).total_seconds()
            except Exception:
                elapsed_seconds = 0

            # 1. Check Kaggle kernel status via CLI
            status_resp = await KaggleService.get_kernel_status(account_username, kernel_ref)
            remote_status = status_resp.get("status", "unknown")

            # 2. Check if run started and notify Telegram
            if (remote_status == "running" or elapsed_seconds > 60) and run.get("telegram_notified_start") == 0:
                await TelegramService.notify_run_started(run)
                update_run_telegram_flag(run_id, "telegram_notified_start", 1)

            # 3. 11-Hour Warning (1 hour before 12-hour limit)
            if elapsed_seconds >= (MAX_KAGGLE_SESSION_SECONDS - WARNING_BEFORE_EXPIRY_SECONDS) and run.get("telegram_notified_11h") == 0:
                await TelegramService.notify_11h_warning(run)
                update_run_telegram_flag(run_id, "telegram_notified_11h", 1)

            # 4. 12-Hour Expiry Alert
            if elapsed_seconds >= MAX_KAGGLE_SESSION_SECONDS and run.get("telegram_notified_12h") == 0:
                await TelegramService.notify_12h_limit_reached(run)
                update_run_telegram_flag(run_id, "telegram_notified_12h", 1)

            # 5. Handle completion or error
            if remote_status in ["complete", "error"]:
                update_run_status(
                    run_id=run_id,
                    status=remote_status,
                    status_message=status_resp.get("raw", ""),
                    end_time=now.isoformat()
                )
                if run.get("telegram_notified_end") == 0:
                    await TelegramService.notify_run_completed(run, remote_status)
                    update_run_telegram_flag(run_id, "telegram_notified_end", 1)
            elif remote_status != "unknown" and remote_status != run["status"]:
                update_run_status(run_id=run_id, status=remote_status)
