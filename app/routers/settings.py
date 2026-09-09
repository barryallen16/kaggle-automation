
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_MODE
from database import get_setting, set_setting
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from services.telegram_service import TelegramService

router = APIRouter(prefix="/api/settings", tags=["Settings"])

VALID_MODES = ("off", "errors-only", "full")


class SettingsPayload(BaseModel):
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    telegram_mode: str | None = None


class TelegramTestPayload(BaseModel):
    bot_token: str | None = None
    chat_id: str | None = None


@router.get("")
async def get_all_settings():
    bot_token = get_setting("telegram_bot_token", TELEGRAM_BOT_TOKEN) or ""
    chat_id = get_setting("telegram_chat_id", TELEGRAM_CHAT_ID) or ""
    mode = (get_setting("telegram_mode", TELEGRAM_MODE) or "errors-only").strip().lower()
    if mode not in VALID_MODES:
        mode = "errors-only"

    masked_token = (
        bot_token[:8] + "..." + bot_token[-5:]
        if len(bot_token) > 15
        else ("***" if bot_token else "")
    )

    return {
        "success": True,
        "settings": {
            "telegram_bot_token_masked": masked_token,
            "telegram_bot_token_configured": bool(bot_token),
            "telegram_chat_id": chat_id,
            "telegram_mode": mode,
        },
    }


@router.get("/mode")
async def get_telegram_mode():
    mode = (get_setting("telegram_mode", TELEGRAM_MODE) or "errors-only").strip().lower()
    return {"mode": mode if mode in VALID_MODES else "errors-only"}


@router.post("")
async def save_settings(payload: SettingsPayload):
    if payload.telegram_bot_token is not None:
        set_setting("telegram_bot_token", payload.telegram_bot_token.strip())
    if payload.telegram_chat_id is not None:
        set_setting("telegram_chat_id", payload.telegram_chat_id.strip())
    if payload.telegram_mode is not None:
        mode = payload.telegram_mode.strip().lower()
        if mode not in VALID_MODES:
            raise HTTPException(status_code=400, detail="Invalid telegram_mode")
        set_setting("telegram_mode", mode)

    return {"success": True, "message": "Settings saved successfully."}


@router.post("/telegram/test")
async def test_telegram(payload: TelegramTestPayload):
    result = await TelegramService.send_test_message(payload.bot_token, payload.chat_id)
    if not result.get("success"):
        raise HTTPException(
            status_code=400, detail=result.get("error", "Failed to send test message")
        )
    return result
