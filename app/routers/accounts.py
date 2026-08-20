from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from app.services.account_manager import AccountManager
from app.database import get_all_accounts, get_active_runs

router = APIRouter(prefix="/api/accounts", tags=["Accounts"])

class AddAccountRequest(BaseModel):
    api_key: str
    username: Optional[str] = None

@router.get("")
async def list_accounts():
    accounts = get_all_accounts()
    active_runs = get_active_runs()
    
    # Map active runs to accounts
    active_by_user = {}
    for r in active_runs:
        user = r["account_username"]
        if user not in active_by_user:
            active_by_user[user] = []
        active_by_user[user].append(r)

    results = []
    for acc in accounts:
        user = acc["username"]
        acc_copy = dict(acc)
        # Hide full API key in response for security
        acc_copy["api_key_masked"] = acc["api_key"][:6] + "..." + acc["api_key"][-4:] if len(acc["api_key"]) > 10 else "***"
        del acc_copy["api_key"]
        acc_copy["active_runs"] = active_by_user.get(user, [])
        results.append(acc_copy)

    return {"success": True, "accounts": results}

@router.post("")
async def add_account(payload: AddAccountRequest):
    if not payload.api_key.strip():
        raise HTTPException(status_code=400, detail="API Key / Token is required")
    
    try:
        result = await AccountManager.add_account(payload.api_key, payload.username)
        return {"success": True, "account": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/{username}")
async def remove_account(username: str):
    try:
        AccountManager.remove_account(username)
        return {"success": True, "message": f"Account {username} removed successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/refresh")
async def refresh_all():
    try:
        updated = await AccountManager.refresh_all_quotas()
        return {"success": True, "accounts": updated}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/{username}/refresh")
async def refresh_single(username: str):
    try:
        quota = await AccountManager.refresh_account_quota(username)
        return {"success": True, "quota": quota}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
