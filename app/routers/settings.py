from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from app.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from app.database import get_setting, set_setting
from app.services.telegram_service import TelegramService

router = APIRouter(prefix="/api/settings", tags=["Settings"])

class SettingsPayload(BaseModel):
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None

class TelegramTestPayload(BaseModel):
    bot_token: Optional[str] = None
    chat_id: Optional[str] = None

@router.get("")
async def get_all_settings():
    bot_token = get_setting("telegram_bot_token", TELEGRAM_BOT_TOKEN) or ""
    chat_id = get_setting("telegram_chat_id", TELEGRAM_CHAT_ID) or ""
    
    masked_token = bot_token[:8] + "..." + bot_token[-5:] if len(bot_token) > 15 else ("***" if bot_token else "")

    return {
        "success": True,
        "settings": {
            "telegram_bot_token_masked": masked_token,
            "telegram_bot_token_configured": bool(bot_token),
            "telegram_chat_id": chat_id
        }
    }

@router.post("")
async def save_settings(payload: SettingsPayload):
    if payload.telegram_bot_token is not None:
        set_setting("telegram_bot_token", payload.telegram_bot_token.strip())
    if payload.telegram_chat_id is not None:
        set_setting("telegram_chat_id", payload.telegram_chat_id.strip())
    
    return {"success": True, "message": "Settings saved successfully."}

@router.post("/telegram/test")
async def test_telegram(payload: TelegramTestPayload):
    result = await TelegramService.send_test_message(payload.bot_token, payload.chat_id)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "Failed to send test message"))
    return result
