from typing import Annotated, Any

from database import get_active_runs, get_all_runs, get_run_by_id, update_run_status
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from services.account_manager import AccountManager
from services.kaggle_service import KaggleService
from services.ops_tracker import tracker

router = APIRouter(prefix="/api/runs", tags=["Runs"])


def _is_gpu_accelerator(acc: Any) -> bool:
    a = str(acc or "").lower()
    return bool(a) and a not in ("none", "default", "cpu")


async def _quota_capped_env(
    account_username: str, accelerator: Any, env_vars: dict[str, str] | None
) -> dict[str, str] | None:
    """Merges a quota-aware MAX_RUNTIME_MINUTES into env_vars for GPU launches.

    The kernel self-finishes before the account's remaining weekly GPU quota
    runs out (Kaggle hangs spent-quota sessions instead of stopping them; a
    stop-stub would lose the version's output). User-pinned env vars win.
    concurrent = this kernel + currently-active GPU runs on the account.
    """
    if not _is_gpu_accelerator(accelerator):
        return env_vars
    active = [
        r
        for r in get_active_runs()
        if r.get("account_username") == account_username
        and _is_gpu_accelerator(r.get("accelerator"))
    ]
    budget = await AccountManager.gpu_runtime_budget_minutes(
        account_username, 1 + len(active)
    )
    if not budget or (env_vars and "MAX_RUNTIME_MINUTES" in env_vars):
        return env_vars
    return {**(env_vars or {}), "MAX_RUNTIME_MINUTES": str(budget)}


class LaunchRunJSONRequest(BaseModel):
    account_username: str
    title: str
    code_content: str
    filename: str = "notebook.ipynb"
    accelerator: str = "none"  # "nvidia-tesla-t4-x2", "nvidia-tesla-t4", "v3-8", "none"
    enable_internet: bool = True
    is_trial: bool = False
    timeout_seconds: int | None = None
    env_vars: dict[str, str] | None = None  # injected as os.environ before user code


@router.get("")
async def list_runs(limit: int = 100):
    limit = max(1, min(limit, 500))
    runs = get_all_runs(limit=limit)
    return {"success": True, "runs": runs}


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
