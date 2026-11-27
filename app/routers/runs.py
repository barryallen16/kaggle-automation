from typing import Annotated, Any

from config import KAGGLE_MAX_GPU_SESSIONS_PER_ACCOUNT, is_gpu_accelerator
from database import (
    get_active_runs,
    get_all_runs,
    get_run_by_id,
    get_runs_count,
    get_session_holding_runs,
    get_total_gpu_hours,
    update_run_status,
)
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from services import availability
from services.account_manager import AccountManager
from services.kaggle_service import KaggleService
from services.ops_tracker import tracker

router = APIRouter(prefix="/api/runs", tags=["Runs"])


async def _quota_capped_env(
    account_username: str, accelerator: Any, env_vars: dict[str, str] | None
) -> dict[str, str] | None:
    if not is_gpu_accelerator(accelerator):
        return env_vars
    active = AccountManager.active_gpu_runs(account_username)
    budget = await AccountManager.gpu_runtime_budget_minutes(
        account_username, 1 + len(active)
    )
    if not budget or (env_vars and "MAX_RUNTIME_MINUTES" in env_vars):
        return env_vars
    return {**(env_vars or {}), "MAX_RUNTIME_MINUTES": str(budget)}


async def _session_cap_rejection(
    account_username: str, accelerator: str
) -> dict[str, Any] | None:
    if not is_gpu_accelerator(accelerator):
        return None
    acc = AccountManager.resolve_effective_username(account_username)
    busy = await availability.live_busy_sessions([acc], True)
    if availability.free_slots(1, busy.get(acc, 0), True) <= 0:
        return {
            "success": False,
            "status": "session_cap",
            "error": await _cap_error_message(acc, busy.get(acc, 0)),
            "conflict_run_id": None,
            "run_id": None,
        }
    return None


async def _cap_error_message(account: str, busy_count: int) -> str:
    cap = KAGGLE_MAX_GPU_SESSIONS_PER_ACCOUNT
    base = f"'{account}' has no free GPU session ({busy_count} in use, limit {cap})."
    held_note = availability.format_held_sessions(
        await availability.held_version_sessions(account)
    )
    stuck = [
        r for r in get_session_holding_runs() if r.get("account_username") == account
    ]
    if not stuck:
        return (
            base
            + " Stop a running kernel on that account or pick another one."
            + held_note
        )
    refs = "\n".join(f"  - {r['kernel_ref']}" for r in stuck[:10])
    more = f"\n  ...and {len(stuck) - 10} more" if len(stuck) > 10 else ""
    return (
        f"{base} {len(stuck)} of them are stuck tearing down (CANCEL_ACKNOWLEDGED) - "
        f"the run is finished but Kaggle still counts the session, and it will not "
        f"clear on its own:\n{refs}{more}\n"
        f"Release them with:\n"
        f"  for r in "
        + " ".join(f"'{r['kernel_ref'].split('/', 1)[-1]}'" for r in stuck[:10])
        + "; do "
        f"kaggle kernels delete -y '{account}/$r'; done"
        + held_note
    )


class LaunchRunJSONRequest(BaseModel):
    account_username: str
    title: str
    code_content: str
    filename: str = "notebook.ipynb"
    accelerator: str = "none"
    enable_internet: bool = True
    is_trial: bool = False
    timeout_seconds: int | None = None
    env_vars: dict[str, str] | None = None


@router.get("")
async def list_runs(limit: int = 100):
    limit = max(1, min(limit, 500))
    runs = get_all_runs(limit=limit)
    return {
        "success": True,
        "runs": runs,
        "total": get_runs_count(),
        "total_gpu_hours": get_total_gpu_hours(),
    }


@router.get("/active")
async def list_active_runs():
    active = get_active_runs()
    return {"success": True, "active_runs": active}


@router.get("/{run_id}")
async def get_run_details(run_id: str):
    run = get_run_by_id(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return {"success": True, "run": run}


@router.post("/launch-json")
async def launch_run_json(payload: LaunchRunJSONRequest):
    tracker.begin("launch_run")
    try:
        async with KaggleService._get_account_lock(payload.account_username):
            capped = await _session_cap_rejection(
                payload.account_username, payload.accelerator
            )
            if capped:
                return capped
            env_vars = await _quota_capped_env(
                payload.account_username, payload.accelerator, payload.env_vars
            )
            result = await KaggleService.push_kernel(
                account_username=payload.account_username,
                title=payload.title,
                code_content=payload.code_content,
                filename=payload.filename,
                accelerator=payload.accelerator,
                enable_internet=payload.enable_internet,
                is_trial=payload.is_trial,
                timeout_seconds=payload.timeout_seconds,
                env_vars=env_vars,
            )
            return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        tracker.end("launch_run")


@router.post("/upload-and-launch")
async def upload_and_launch(
    file: Annotated[UploadFile, File()],
    account_username: str = Form(...),
    title: str = Form(...),
    accelerator: str = Form("none"),
    enable_internet: bool = Form(True),
    is_trial: bool = Form(False),
    timeout_seconds: int | None = Form(None),
):
    tracker.begin("launch_run")
    try:
        content_bytes = await file.read()
        code_content = content_bytes.decode("utf-8", errors="ignore")
        filename = file.filename or "notebook.ipynb"

        async with KaggleService._get_account_lock(account_username):
            capped = await _session_cap_rejection(account_username, accelerator)
            if capped:
                return capped
            env_vars = await _quota_capped_env(account_username, accelerator, None)
            result = await KaggleService.push_kernel(
                account_username=account_username,
                title=title,
                code_content=code_content,
                filename=filename,
                accelerator=accelerator,
                enable_internet=enable_internet,
                is_trial=is_trial,
                timeout_seconds=timeout_seconds,
                env_vars=env_vars,
            )
            return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        tracker.end("launch_run")


@router.post("/{run_id}/stop")
async def stop_run(run_id: str):
    try:
        result = await KaggleService.stop_kernel(run_id)
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{run_id}/refresh-status")
async def refresh_single_status(run_id: str):
    run = get_run_by_id(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    status_resp = await KaggleService.get_kernel_status(
        run["account_username"], run["kernel_ref"]
    )
    new_status = status_resp.get("status", run["status"])
    if new_status != "unknown" and new_status != run["status"]:
        update_run_status(run_id, new_status, status_resp.get("raw", ""))

    updated_run = get_run_by_id(run_id)
    return {"success": True, "status": new_status, "run": updated_run}
