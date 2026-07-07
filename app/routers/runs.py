from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from app.services.kaggle_service import KaggleService
from app.database import get_all_runs, get_active_runs, get_run_by_id, update_run_status

router = APIRouter(prefix="/api/runs", tags=["Runs"])

class LaunchRunJSONRequest(BaseModel):
    account_username: str
    title: str
    code_content: str
    filename: str = "notebook.ipynb"
    accelerator: str = "none" # "nvidia-tesla-t4-x2", "nvidia-tesla-t4", "v3-8", "none"
    enable_internet: bool = True
    is_trial: bool = False
    timeout_seconds: Optional[int] = None

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
    try:
        result = await KaggleService.push_kernel(
            account_username=payload.account_username,
            title=payload.title,
            code_content=payload.code_content,
            filename=payload.filename,
            accelerator=payload.accelerator,
            enable_internet=payload.enable_internet,
            is_trial=payload.is_trial,
            timeout_seconds=payload.timeout_seconds
        )
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/upload-and-launch")
async def upload_and_launch(
    file: UploadFile = File(...),
    account_username: str = Form(...),
    title: str = Form(...),
    accelerator: str = Form("none"),
    enable_internet: bool = Form(True),
    is_trial: bool = Form(False),
    timeout_seconds: Optional[int] = Form(None)
):
    try:
        content_bytes = await file.read()
        code_content = content_bytes.decode("utf-8", errors="ignore")
        filename = file.filename or "notebook.ipynb"

        result = await KaggleService.push_kernel(
            account_username=account_username,
            title=title,
            code_content=code_content,
            filename=filename,
            accelerator=accelerator,
            enable_internet=enable_internet,
            is_trial=is_trial,
            timeout_seconds=timeout_seconds
        )
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

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
    
    status_resp = await KaggleService.get_kernel_status(run["account_username"], run["kernel_ref"])
    new_status = status_resp.get("status", run["status"])
    if new_status != "unknown" and new_status != run["status"]:
        update_run_status(run_id, new_status, status_resp.get("raw", ""))
    
    updated_run = get_run_by_id(run_id)
    return {"success": True, "status": new_status, "run": updated_run}
