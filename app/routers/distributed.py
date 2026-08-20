from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
import json
from app.services.workload_distributor import WorkloadDistributor
from app.database import get_all_workloads, get_all_runs

router = APIRouter(prefix="/api/distributed", tags=["Distributed Workload"])

class DistributedLaunchJSON(BaseModel):
    base_title: str
    code_content: str
    filename: str = "notebook.ipynb"
    accounts: List[str]
    total_items: int = 10000000
    start_offset: int = 0
    accelerator: str = "none"
    enable_internet: bool = True
    is_trial: bool = False
    timeout_seconds: Optional[int] = None

@router.get("")
async def list_workloads():
    workloads = get_all_workloads()
    all_runs = get_all_runs(limit=500)
    
    # Attach runs to workloads
    for w in workloads:
        w_id = w["id"]
        w["shards"] = [r for r in all_runs if r.get("workload_id") == w_id]

    return {"success": True, "workloads": workloads}

@router.post("/launch-json")
async def launch_distributed_json(payload: DistributedLaunchJSON):
    if len(payload.accounts) < 1:
        raise HTTPException(status_code=400, detail="At least one Kaggle account must be selected.")

    try:
        result = await WorkloadDistributor.distribute_and_launch(
            base_title=payload.base_title,
            code_content=payload.code_content,
            filename=payload.filename,
            accounts=payload.accounts,
            total_items=payload.total_items,
            start_offset=payload.start_offset,
            accelerator=payload.accelerator,
            enable_internet=payload.enable_internet,
            is_trial=payload.is_trial,
            timeout_seconds=payload.timeout_seconds
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/upload-and-launch")
async def upload_and_launch_distributed(
    file: UploadFile = File(...),
    base_title: str = Form(...),
    accounts: str = Form(...), # JSON array string or comma separated
    total_items: int = Form(10000000),
    start_offset: int = Form(0),
    accelerator: str = Form("none"),
    enable_internet: bool = Form(True),
    is_trial: bool = Form(False),
    timeout_seconds: Optional[int] = Form(None)
):
    try:
        # Parse accounts list
        if accounts.strip().startswith("["):
            acc_list = json.loads(accounts)
        else:
            acc_list = [a.strip() for a in accounts.split(",") if a.strip()]

        if not acc_list:
            raise HTTPException(status_code=400, detail="Please select at least 1 Kaggle account.")

        content_bytes = await file.read()
        code_content = content_bytes.decode("utf-8", errors="ignore")
        filename = file.filename or "notebook.ipynb"

        result = await WorkloadDistributor.distribute_and_launch(
            base_title=base_title,
            code_content=code_content,
            filename=filename,
            accounts=acc_list,
            total_items=total_items,
            start_offset=start_offset,
            accelerator=accelerator,
            enable_internet=enable_internet,
            is_trial=is_trial,
            timeout_seconds=timeout_seconds
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
