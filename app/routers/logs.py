from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import PlainTextResponse, FileResponse
from pathlib import Path
import asyncio
from app.services.kaggle_service import KaggleService
from app.database import get_run_by_id
from app.config import LOGS_DIR

router = APIRouter(tags=["Logs"])

@router.get("/api/runs/{run_id}/logs")
async def get_logs(run_id: str, fetch_remote: bool = False):
    run = get_run_by_id(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    log_path = LOGS_DIR / f"{run_id}.log"
    local_log = ""
    if log_path.exists():
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            local_log = f.read()

    if fetch_remote:
        remote_logs = await KaggleService.fetch_full_logs(run["account_username"], run["kernel_ref"])
        if remote_logs and not remote_logs.startswith("Error"):
            local_log += "\n--- Remote Logs from Kaggle API ---\n" + remote_logs
            with open(log_path, "w", encoding="utf-8") as f:
                f.write(local_log)

    return PlainTextResponse(content=local_log)

@router.get("/api/runs/{run_id}/logs/download")
async def download_log_file(run_id: str):
    log_path = LOGS_DIR / f"{run_id}.log"
    if not log_path.exists():
        raise HTTPException(status_code=404, detail="Log file does not exist")
    return FileResponse(path=str(log_path), filename=f"{run_id}_logs.txt", media_type="text/plain")

@router.websocket("/ws/runs/{run_id}/logs")
async def websocket_logs(websocket: WebSocket, run_id: str):
    await websocket.accept()
    
    # Send existing log file contents first
    log_path = LOGS_DIR / f"{run_id}.log"
    if log_path.exists():
        try:
            with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                initial_lines = f.read()
                if initial_lines:
                    await websocket.send_text(initial_lines)
        except Exception:
            pass

    # Subscribe to live streaming log queue
    queue = KaggleService.register_log_subscriber(run_id)
    
    try:
        while True:
            # Check for incoming messages or ping/pong
            try:
                line = await asyncio.wait_for(queue.get(), timeout=1.0)
                await websocket.send_text(line)
            except asyncio.TimeoutError:
                # Keepalive heartbeat
                await websocket.send_text("")
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        KaggleService.unregister_log_subscriber(run_id, queue)
