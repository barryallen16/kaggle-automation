import html
import httpx
import logging
from typing import Optional, Dict, Any
from app.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from app.database import get_setting

logger = logging.getLogger("telegram_service")

class TelegramService:
    @staticmethod
    def get_credentials() -> tuple[str, str]:
        token = get_setting("telegram_bot_token", TELEGRAM_BOT_TOKEN) or ""
        chat_id = get_setting("telegram_chat_id", TELEGRAM_CHAT_ID) or ""
        return token.strip(), chat_id.strip()

    @staticmethod
    def _esc(value: Any) -> str:
        """Escapes user-controlled text for Telegram parse_mode=HTML."""
        return html.escape(str(value if value is not None else ""), quote=True)

    @classmethod
    async def send_message(cls, text: str, parse_mode: str = "HTML") -> Dict[str, Any]:
        """Sends a bot direct-message to the configured Telegram USER ID."""
        token, chat_id = cls.get_credentials()
        if not token or not chat_id:
            logger.warning("Telegram Bot Token or User ID not configured. Message not sent.")
            return {"success": False, "error": "Telegram Bot Token or User ID not configured"}

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": False
        }

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(url, json=payload)
                data = response.json()
                if response.status_code == 200 and data.get("ok"):
                    logger.info("Telegram notification sent successfully")
                    return {"success": True, "data": data}
                else:
                    description = str(data.get("description", "Unknown error"))
                    # Bots cannot start conversations with users - make this failure actionable
                    if "initiate conversation" in description or response.status_code == 403:
                        description += (
                            " | Open the bot in Telegram and press START (send /start) "
                            "once, then try again - bots cannot message users who never started them."
                        )
                    elif "chat not found" in description.lower():
                        description += (
                            " | The User ID looks invalid. Get your numeric ID from @userinfobot."
                        )
                    logger.error(f"Telegram API error: {description}")
                    return {"success": False, "error": description}
        except Exception as e:
            logger.error(f"Failed to send Telegram notification: {str(e)}")
            return {"success": False, "error": str(e)}

    @classmethod
    async def send_test_message(cls, test_token: Optional[str] = None, test_chat_id: Optional[str] = None) -> Dict[str, Any]:
        token = test_token or get_setting("telegram_bot_token", TELEGRAM_BOT_TOKEN) or ""
        chat_id = test_chat_id or get_setting("telegram_chat_id", TELEGRAM_CHAT_ID) or ""
        
        if not token or not chat_id:
            return {"success": False, "error": "Both Bot Token and User ID are required."}

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        text = (
            "🤖 <b>Kaggle Automation Bot Connected!</b>\n\n"
            "You will now receive <b>direct messages</b> from this bot for notebook runs, "
            "11-hour warnings, 12-hour session cutoffs, and completions.\n\n"
            "<i>If you ever stop receiving alerts, make sure you haven't blocked the bot "
            "and that you pressed START when you first opened it.</i>"
        )
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML"
        }

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(url, json=payload)
                data = response.json()
                if response.status_code == 200 and data.get("ok"):
                    return {"success": True, "message": "Test notification delivered successfully!"}
                else:
                    return {"success": False, "error": data.get("description", "Failed to send message")}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @classmethod
    async def notify_run_started(cls, run: Dict[str, Any]):
        trial_badge = "🧪 <b>[TRIAL RUN]</b> " if run.get("is_trial") else "🚀 "
        acc = run.get("accelerator", "Default")
        acc_label = "T4 GPU x 2" if "t4-x2" in acc else ("TPU v3-8" if "v3-8" in acc else ("T4 GPU" if "t4" in acc else "CPU/Default"))
        
        esc = cls._esc
        msg = (
            f"{trial_badge}<b>Notebook Run Started</b>\n\n"
            f"• <b>Notebook:</b> {esc(run.get('title'))}\n"
            f"• <b>Account:</b> @{esc(run.get('account_username'))}\n"
            f"• <b>Accelerator:</b> <code>{esc(acc_label)}</code>\n"
            f"• <b>Max Runtime:</b> 12 Hours\n"
            f"• <b>Kaggle Link:</b> <a href=\"{esc(run.get('kaggle_url'))}\">View on Kaggle</a>\n"
        )
        if run.get("workload_id") and run.get("shard_index") is not None:
            msg += f"• <b>Distributed Shard:</b> {run.get('shard_index') + 1} of {run.get('total_shards')}\n"
        await cls.send_message(msg)

    @classmethod
    async def notify_11h_warning(cls, run: Dict[str, Any]):
        esc = cls._esc
        msg = (
            f"⚠️ <b>1-Hour Warning: Approaching 12h Kaggle Limit!</b>\n\n"
            f"• <b>Notebook:</b> {esc(run.get('title'))}\n"
            f"• <b>Account:</b> @{esc(run.get('account_username'))}\n"
            f"• <b>Running For:</b> ~11 Hours\n"
            f"• <b>Action:</b> Make sure outputs are saved. Kaggle will terminate the session at the 12-hour mark.\n"
            f"• <b>Kaggle Link:</b> <a href=\"{esc(run.get('kaggle_url'))}\">View on Kaggle</a>"
        )
        await cls.send_message(msg)

    @classmethod
    async def notify_12h_limit_reached(cls, run: Dict[str, Any]):
        esc = cls._esc
        msg = (
            f"⏰ <b>12-Hour Continuous Session Cutoff Reached</b>\n\n"
            f"• <b>Notebook:</b> {esc(run.get('title'))}\n"
            f"• <b>Account:</b> @{esc(run.get('account_username'))}\n"
            f"• <b>Session Duration:</b> 12 Hours (Upper limit reached)\n"
            f"• <b>Kaggle Link:</b> <a href=\"{esc(run.get('kaggle_url'))}\">View on Kaggle</a>"
        )
        await cls.send_message(msg)

    @classmethod
    async def notify_run_completed(cls, run: Dict[str, Any], status: str):
        esc = cls._esc
        icon = "✅" if status == "complete" else "❌"
        msg = (
            f"{icon} <b>Notebook Session {esc(status.upper())}</b>\n\n"
            f"• <b>Notebook:</b> {esc(run.get('title'))}\n"
            f"• <b>Account:</b> @{esc(run.get('account_username'))}\n"
            f"• <b>Status:</b> <code>{esc(status)}</code>\n"
            f"• <b>Kaggle Link:</b> <a href=\"{esc(run.get('kaggle_url'))}\">View on Kaggle</a>"
        )
        await cls.send_message(msg)
