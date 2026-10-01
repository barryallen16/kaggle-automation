import asyncio
import json
from typing import Annotated, Any

from database import (
    get_active_runs,
    get_all_runs,
    get_all_workloads,
    get_session_holding_runs,
    update_workload_status,
)
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from services.kaggle_service import KaggleService
from services.ops_tracker import tracker, workload_stop_key
from services.workload_distributor import WorkloadDistributor

router = APIRouter(prefix="/api/distributed", tags=["Distributed Workload"])


class ManualShardItem(BaseModel):
    shard_index: int | None = None
    account: str
    start_index: int
    end_index: int
    custom_params: dict[str, Any] | None = None


def _json_or_400(text: str | None, name: str):
    """json.loads for form fields that accept JSON; None for blank/non-JSON input."""
    text = (text or "").strip()
    if not text or text[0] not in "[{":
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail=f"{name} must be valid JSON")


class DistributedLaunchJSON(BaseModel):
    base_title: str
    code_content: str
    filename: str = "notebook.ipynb"
    accounts: list[str]
    total_items: int = 10000000
    start_offset: int = 0
    accelerator: str = "none"
    enable_internet: bool = True
    is_trial: bool = False
    timeout_seconds: int | None = None
    env_vars: dict[str, str] | None = None
    # Global session count (int) OR per-account overrides {username: 1|2}
    sessions_per_account: int | dict[str, int] = 2
    # Optional manual shards configuration
    manual_shards: list[ManualShardItem] | None = None


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
    """Stops every active shard of a distributed workload in one call.

    Shards stuck in 'cancelling' (CANCEL_ACKNOWLEDGED) are finished for the run
    but still hold a Kaggle session slot, and a stop stub cannot release one -
    so they are NOT stop targets. When every shard ended up that way this used
    to 404 with "No active shards found", which reads as a bug; name what is
    actually holding the slots instead.
    """
    if not any(w["id"] == workload_id for w in get_all_workloads()):
        raise HTTPException(status_code=404, detail=f"Workload {workload_id} not found")

    all_runs = [
        r
        for r in get_active_runs() + get_session_holding_runs()
        if r.get("workload_id") == workload_id
    ]
    targets = [r for r in all_runs if r.get("status") in ("queued", "running")]
    stuck = [r for r in all_runs if r.get("status") == "cancelling"]

    if not targets:
        note = ""
        if stuck:
            refs = ", ".join(sorted({r["kernel_ref"] for r in stuck})[:10])
            note = (
                f" {len(stuck)} shard(s) are stuck tearing down (CANCEL_ACKNOWLEDGED) "
                f"and still hold GPU session slots - a stop stub will not free them. "
                f"Release them with `kaggle kernels delete -y '<ref>'` ({refs})."
            )
        raise HTTPException(
            status_code=409,
            detail=(f"No active shards left to stop for workload {workload_id}.{note}"),
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

    tail = (
        f" {len(stuck)} shard(s) remain stuck tearing down and still hold session slots."
        if stuck
        else ""
    )
    return {
        "success": bool(stopped),
        "workload_id": workload_id,
        "stopped": stopped,
        "failed": failed,
        "message": f"Stopped {len(stopped)}/{len(targets)} shards.{tail}",
    }


@router.post("/upload-and-launch")
async def upload_and_launch_distributed(
    file: Annotated[UploadFile, File()],
    base_title: str = Form(...),
    accounts: str = Form(...),  # JSON array string or comma separated
    total_items: int = Form(10000000),
    start_offset: int = Form(0),
    accelerator: str = Form("none"),
    enable_internet: bool = Form(True),
    is_trial: bool = Form(False),
    timeout_seconds: int | None = Form(None),
    env_vars: str | None = Form(None),
    sessions_per_account: str = Form("2"),  # "2" or JSON object {"user": 2}
):
    try:
        # Accounts: JSON array or comma-separated string
        acc_parsed = _json_or_400(accounts, "accounts")
        if isinstance(acc_parsed, list):
            acc_list = acc_parsed
        elif acc_parsed is None:
            acc_list = [a.strip() for a in accounts.split(",") if a.strip()]
        else:
            raise HTTPException(
                status_code=400,
                detail="accounts JSON must be an array of usernames",
            )

        if not acc_list:
            raise HTTPException(
                status_code=400, detail="Please select at least 1 Kaggle account."
            )

        content_bytes = await file.read()
        code_content = content_bytes.decode("utf-8", errors="ignore")
        filename = file.filename or "notebook.ipynb"

        parsed_env_vars = None
        env_obj = _json_or_400(env_vars, "env_vars")
        if env_obj is not None:
            if not isinstance(env_obj, dict):
                raise HTTPException(
                    status_code=400, detail="env_vars must be a JSON object"
                )
            parsed_env_vars = {str(k): str(v) for k, v in env_obj.items()}

        # Sessions: plain int or per-account JSON object
        sessions_raw = (sessions_per_account or "2").strip()
        sessions_obj = _json_or_400(sessions_raw, "sessions_per_account")
        if isinstance(sessions_obj, dict):
            sessions_val: int | dict[str, int] = sessions_obj
        elif sessions_obj is None:
            try:
                sessions_val = int(sessions_raw)
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail="sessions_per_account must be an int or JSON object",
                )
        else:
            raise HTTPException(
                status_code=400,
                detail="sessions_per_account JSON must be an object",
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
