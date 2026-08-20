from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from pydantic import BaseModel
from typing import Optional, List, Dict, Any, Union
import asyncio
import json
from services.workload_distributor import WorkloadDistributor
from services.kaggle_service import KaggleService
from services.ops_tracker import tracker, workload_stop_key
from database import (
    get_all_workloads,
    get_all_runs,
    get_active_runs,
    update_workload_status,
)

router = APIRouter(prefix="/api/distributed", tags=["Distributed Workload"])


class ManualShardItem(BaseModel):
    shard_index: Optional[int] = None
    account: str
    start_index: int
    end_index: int
    custom_params: Optional[Dict[str, Any]] = None


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
    env_vars: Optional[Dict[str, str]] = None
    # Global session count (int) OR per-account overrides {username: 1|2}
    sessions_per_account: Union[int, Dict[str, int]] = 2
    # Optional manual shards configuration
    manual_shards: Optional[List[ManualShardItem]] = None


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
        raise HTTPException(
            status_code=400, detail="At least one Kaggle account must be selected."
        )

    if tracker.is_active("distribute"):
        raise HTTPException(
            status_code=409,
            detail="A distributed launch is already in progress - wait for it to finish first.",
        )
    tracker.begin("distribute")
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
            timeout_seconds=payload.timeout_seconds,
            env_vars=payload.env_vars,
            sessions_per_account=payload.sessions_per_account,
            manual_shards=[s.dict() for s in payload.manual_shards]
            if payload.manual_shards
            else None,
        )
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        tracker.end("distribute")


@router.post("/{workload_id}/stop")
async def stop_workload(workload_id: str):
    """Stops every active shard of a distributed workload in one call."""
    targets = [r for r in get_active_runs() if r.get("workload_id") == workload_id]
    if not targets:
        raise HTTPException(
            status_code=404, detail=f"No active shards found for workload {workload_id}"
        )

    if tracker.is_active(workload_stop_key(workload_id)):
        return {
            "success": False,
            "detail": "Stop is already in progress for this workload - please wait.",
            "workload_id": workload_id,
        }
    tracker.begin(workload_stop_key(workload_id))
    try:
        results = await asyncio.gather(
            *[KaggleService.stop_kernel(r["id"]) for r in targets],
            return_exceptions=True,
        )
    finally:
        tracker.end(workload_stop_key(workload_id))
    stopped, failed = [], []
    for r, res in zip(targets, results):
        if isinstance(res, Exception):
            failed.append({"run_id": r["id"], "error": str(res)})
        elif isinstance(res, dict) and res.get("success"):
            stopped.append(r["id"])
        else:
            failed.append(
                {"run_id": r["id"], "error": (res or {}).get("error", "unknown")}
            )

    if stopped and not failed:
        update_workload_status(workload_id, "stopped")
    elif stopped:
        update_workload_status(workload_id, "partial")

    return {
        "success": bool(stopped),
        "workload_id": workload_id,
        "stopped": stopped,
        "failed": failed,
        "message": f"Stopped {len(stopped)}/{len(targets)} shards.",
    }


@router.post("/upload-and-launch")
async def upload_and_launch_distributed(
    file: UploadFile = File(...),
    base_title: str = Form(...),
    accounts: str = Form(...),  # JSON array string or comma separated
    total_items: int = Form(10000000),
    start_offset: int = Form(0),
    accelerator: str = Form("none"),
    enable_internet: bool = Form(True),
    is_trial: bool = Form(False),
    timeout_seconds: Optional[int] = Form(None),
    env_vars: Optional[str] = Form(None),
    sessions_per_account: str = Form("2"),  # "2" or JSON object {"user": 2}
):
    try:
        # Parse accounts list
        if accounts.strip().startswith("["):
            try:
                acc_list = json.loads(accounts)
            except json.JSONDecodeError:
                raise HTTPException(
                    status_code=400,
                    detail="accounts must be a valid JSON array or comma-separated string",
                )
            if not isinstance(acc_list, list):
                raise HTTPException(
                    status_code=400,
                    detail="accounts JSON must be an array of usernames",
                )
        else:
            acc_list = [a.strip() for a in accounts.split(",") if a.strip()]

        if not acc_list:
            raise HTTPException(
                status_code=400, detail="Please select at least 1 Kaggle account."
            )

        content_bytes = await file.read()
        code_content = content_bytes.decode("utf-8", errors="ignore")
        filename = file.filename or "notebook.ipynb"

        parsed_env_vars = None
        if env_vars and env_vars.strip():
            try:
                obj = json.loads(env_vars)
                if isinstance(obj, dict):
                    parsed_env_vars = {str(k): str(v) for k, v in obj.items()}
                else:
                    raise HTTPException(
                        status_code=400, detail="env_vars must be a JSON object"
                    )
            except json.JSONDecodeError:
                raise HTTPException(
                    status_code=400, detail="env_vars must be valid JSON"
                )

        # Sessions: plain int or per-account JSON object
        sessions_raw = (sessions_per_account or "2").strip()
        if sessions_raw.startswith("{"):
            try:
                sessions_obj = json.loads(sessions_raw)
                if not isinstance(sessions_obj, dict):
                    raise HTTPException(
                        status_code=400,
                        detail="sessions_per_account JSON must be an object",
                    )
            except json.JSONDecodeError:
                raise HTTPException(
                    status_code=400,
                    detail="sessions_per_account must be an int or JSON object",
                )
            sessions_val: Union[int, Dict[str, int]] = sessions_obj
        else:
            try:
                sessions_val = int(sessions_raw)
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail="sessions_per_account must be an int or JSON object",
                )

        if tracker.is_active("distribute"):
            raise HTTPException(
                status_code=409,
                detail="A distributed launch is already in progress - wait for it to finish first.",
            )
        tracker.begin("distribute")
        try:
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
                timeout_seconds=timeout_seconds,
                env_vars=parsed_env_vars,
                sessions_per_account=sessions_val,
            )
            return result
        finally:
            tracker.end("distribute")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
