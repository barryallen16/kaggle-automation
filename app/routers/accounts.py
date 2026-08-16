from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from app.services.account_manager import AccountManager
from app.services.ops_tracker import tracker
from app.database import get_all_accounts, get_active_runs

router = APIRouter(prefix="/api/accounts", tags=["Accounts"])

class AddAccountRequest(BaseModel):
    api_key: str
    username: Optional[str] = None

def _mask_key(key: str) -> str:
    return key[:6] + "..." + key[-4:] if len(key) > 10 else "***"

def _sanitize_account(acc: Dict[str, Any]) -> Dict[str, Any]:
    acc_copy = dict(acc)
    acc_copy["api_key_masked"] = _mask_key(acc.get("api_key", ""))
    acc_copy.pop("api_key", None)
    return acc_copy

def _account_quota_left(acc: Dict[str, Any]) -> tuple:
    last_q = acc.get("last_quota") or {}
    if not isinstance(last_q, dict):
        return (0.0, 0.0)
    gpu = last_q.get("gpu") or {}
    tpu = last_q.get("tpu") or {}
    try:
        gpu_limit = float(gpu.get("limit") if gpu.get("limit") is not None else 30.0)
        gpu_used = float(gpu.get("used") if gpu.get("used") is not None else 0.0)
        gpu_left = max(0.0, gpu_limit - gpu_used)
    except (ValueError, TypeError):
        gpu_left = 0.0
    try:
        tpu_limit = float(tpu.get("limit") if tpu.get("limit") is not None else 20.0)
        tpu_used = float(tpu.get("used") if tpu.get("used") is not None else 0.0)
        tpu_left = max(0.0, tpu_limit - tpu_used)
    except (ValueError, TypeError):
        tpu_left = 0.0
    return (gpu_left, tpu_left)

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
        acc_copy = _sanitize_account(acc)
        acc_copy["active_runs"] = active_by_user.get(user, [])
        results.append(acc_copy)

    # Sort descending by quota left: GPU hours first, then TPU hours, then username
    results.sort(
        key=lambda a: (_account_quota_left(a)[0], _account_quota_left(a)[1], a.get("username", "")),
        reverse=True
    )

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
    tracker.begin("refresh_quotas")
    try:
        updated = await AccountManager.refresh_all_quotas()
        return {"success": True, "accounts": [_sanitize_account(a) for a in updated]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        tracker.end("refresh_quotas")

@router.post("/{username}/refresh")
async def refresh_single(username: str):
    tracker.begin("refresh_quotas")
    try:
        quota = await AccountManager.refresh_account_quota(username)
        return {"success": True, "quota": quota}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        tracker.end("refresh_quotas")

@router.get("/{username}/debug")
async def debug_account(username: str):
    """Debug fallback usernames like kaggle_0694f485 - shows real Kaggle username via JWT and via CLI."""
    from app.database import get_account_by_username
    from app.services.account_manager import AccountManager
    import base64, json as _json
    acc = get_account_by_username(username)
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
    key = acc.get("api_key", "")
    # Try JWT decode
    jwt_username = None
    try:
        parts = key.strip().split(".")
        if len(parts) == 3:
            payload = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
            decoded = base64.urlsafe_b64decode(payload)
            claims = _json.loads(decoded)
            for field in ["username", "user_name", "sub", "preferred_username"]:
                if field in claims and isinstance(claims[field], str) and not claims[field].isdigit():
                    jwt_username = claims[field]
                    break
    except Exception:
        pass
    # Try CLI discovery (may be slow)
    cli_username = None
    cli_error = None
    try:
        # Use a temp id to avoid colliding with real account
        import uuid
        temp_id = f"debug_{uuid.uuid4().hex[:4]}"
        cli_username = await AccountManager.fetch_username_for_key(temp_id, key)
        AccountManager._cleanup_temp_dir(temp_id)
    except Exception as e:
        cli_error = str(e)
    return {
        "success": True,
        "stored_username": username,
        "is_fallback": username.startswith("kaggle_"),
        "jwt_username": jwt_username,
        "cli_discovered_username": cli_username,
        "cli_error": cli_error,
        "api_key_prefix": key[:15] + "..." if len(key) > 15 else "***",
    }
