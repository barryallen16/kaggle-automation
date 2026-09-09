import html
import logging
from typing import Any, ClassVar

import httpx
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_MODE
from database import get_setting

logger = logging.getLogger("telegram_service")

VALID_MODES = ("off", "errors-only", "full")


class TelegramService:
    # ponytail: in-memory 11h dedup per workload (no DB/persistence).
    # Ceiling: restart resends one warning per workload; upgrade to DB flag if noisy.
    _warned_workloads: ClassVar[set[str]] = set()

    @staticmethod
    def get_credentials() -> tuple[str, str]:
        token = get_setting("telegram_bot_token", TELEGRAM_BOT_TOKEN) or ""
        chat_id = get_setting("telegram_chat_id", TELEGRAM_CHAT_ID) or ""
        return token.strip(), chat_id.strip()

    @staticmethod
    def get_mode() -> str:
        mode = (get_setting("telegram_mode", TELEGRAM_MODE) or "errors-only").strip().lower()
        return mode if mode in VALID_MODES else "errors-only"

    @staticmethod
    def should_send(event: str) -> bool:
        mode = TelegramService.get_mode()
        if mode == "off":
            return False
        if mode == "full":
            return True
        return event in ("11h", "failed")  # errors-only default

    @staticmethod
    def _esc(value: Any) -> str:
        """Escapes user-controlled text for Telegram parse_mode=HTML."""
        return html.escape(str(value if value is not None else ""), quote=True)

    @classmethod
    async def _post(cls, token: str, chat_id: str, text: str) -> dict[str, Any]:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(
                    url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
                )
                data = response.json()
                if response.status_code == 200 and data.get("ok"):
                    logger.info("Telegram notification sent successfully")
                    return {"success": True, "data": data}
                description = str(data.get("description", "Unknown error"))
                if "initiate conversation" in description or response.status_code == 403:
                    description += (
                        " | Open the bot in Telegram and press START once,"
                        " then try again."
                    )
                elif "chat not found" in description.lower():
                    description += " | Get your numeric ID from @userinfobot."
                logger.error(f"Telegram API error: {description}")
                return {"success": False, "error": description}
        except Exception as e:
            logger.error(f"Failed to send Telegram notification: {e!s}")
            return {"success": False, "error": str(e)}

    @classmethod
    async def send_message(cls, text: str) -> dict[str, Any]:
        """Sends a bot direct-message to the configured Telegram USER ID."""
        token, chat_id = cls.get_credentials()
        if not token or not chat_id:
            logger.warning("Telegram not configured. Message not sent.")
            return {"success": False, "error": "Telegram Bot Token or User ID not configured"}
        return await cls._post(token, chat_id, text)

    @classmethod
    async def send_test_message(
        cls, test_token: str | None = None, test_chat_id: str | None = None
    ) -> dict[str, Any]:
        token = test_token or get_setting("telegram_bot_token", TELEGRAM_BOT_TOKEN) or ""
        chat_id = test_chat_id or get_setting("telegram_chat_id", TELEGRAM_CHAT_ID) or ""
        if not token or not chat_id:
            return {"success": False, "error": "Both Bot Token and User ID are required."}
        return await cls._post(
            token.strip(),
            chat_id.strip(),
            "🤖 <b>Kaggle Automation Bot Connected!</b>\n\n"
            "Errors-only mode: you get failures, quota-kills and the 11h warning."
            " Switch to Full in Settings for start/finish alerts.",
        )

    @classmethod
    def _shard_line(cls, run: dict[str, Any]) -> str:
        if run.get("workload_id") and run.get("shard_index") is not None:
            return f"• <b>Shard:</b> {run.get('shard_index') + 1} of {run.get('total_shards')}\n"
        return ""

    @classmethod
    def _build(cls, event: str, run: dict[str, Any], detail: str = "") -> str:
        esc = cls._esc
        link = f"<a href=\"{esc(run.get('kaggle_url'))}\">View on Kaggle</a>"
        head = (
            f"• <b>Notebook:</b> {esc(run.get('title'))}\n"
            f"• <b>Account:</b> @{esc(run.get('account_username'))}\n"
            f"{cls._shard_line(run)}"
        )
        if event == "started":
            acc = str(run.get("accelerator", "Default")).lower()
            label = "T4 GPU x 2" if "t4-x2" in acc else ("TPU v3-8" if "v3-8" in acc else ("T4 GPU" if "t4" in acc else "CPU/Default"))
            badge = "🧪 <b>[TRIAL]</b> " if run.get("is_trial") else "🚀 "
            return f"{badge}<b>Run Started</b> (<code>{esc(label)}</code>)\n\n{head}• {link}"
        if event == "11h":
            return (
                "⚠️ <b>1h left (11/12h) — save outputs now</b>\n\n"
                f"{head}• {link}"
            )
        if event == "failed":
            err = esc(str(detail or run.get("status_message") or "unknown")[:300])
            return f"❌ <b>Run FAILED</b>\n\n{head}• <b>Error:</b> <code>{err}</code>\n• {link}"
        return f"✅ <b>Run complete</b>\n\n{head}• {link}"

    @classmethod
    async def notify(
        cls, event: str, run: dict[str, Any], detail: str = ""
    ) -> dict[str, Any]:
        """Single entry point. Events: started | 11h | failed | complete."""
        if not cls.should_send(event):
            return {"success": True, "suppressed": True}
        wid = run.get("workload_id")
        if event == "11h" and wid:
            if wid in cls._warned_workloads:
                return {"success": True, "suppressed": True}
            cls._warned_workloads.add(wid)
        await cls.send_message(cls._build(event, run, detail))
        return {"success": True}
