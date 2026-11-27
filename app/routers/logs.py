import asyncio
import re
from pathlib import Path

from config import LOGS_DIR
from database import get_run_by_id
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse
from services.kaggle_service import KaggleService

router = APIRouter(tags=["Logs"])

SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


def _safe_log_path(run_id: str) -> Path:
    if not SAFE_ID_RE.match(run_id or ""):
        raise HTTPException(status_code=400, detail="Invalid run id")
    return LOGS_DIR / f"{run_id}.log"


def _read_log_lossy(path: Path) -> str:
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()


def _write_log(path: Path, content: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


@router.get("/api/runs/{run_id}/logs")
async def get_logs(run_id: str, fetch_remote: bool = False):
    run = get_run_by_id(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    log_path = _safe_log_path(run_id)
    local_log = ""
    if log_path.exists():
        local_log = await asyncio.to_thread(_read_log_lossy, log_path)

    if fetch_remote:
        remote_logs = await KaggleService.fetch_full_logs(
            run["account_username"], run["kernel_ref"]
        )
        if remote_logs and not remote_logs.startswith("Error"):
            local_log += "\n--- Remote Logs from Kaggle API ---\n" + remote_logs
            await asyncio.to_thread(_write_log, log_path, local_log)

    return PlainTextResponse(content=local_log)


@router.get("/api/runs/{run_id}/logs/download")
async def download_log_file(run_id: str):
    log_path = _safe_log_path(run_id)
    if not log_path.exists():
        raise HTTPException(status_code=404, detail="Log file does not exist")
    return FileResponse(
        path=str(log_path), filename=f"{run_id}_logs.txt", media_type="text/plain"
    )


@router.websocket("/ws/runs/{run_id}/logs")
async def websocket_logs(websocket: WebSocket, run_id: str, skip_initial: bool = False):
    import auth as auth_mod
    from config import APP_AUTH_TOKEN

    if APP_AUTH_TOKEN:
        cookie = websocket.cookies.get(auth_mod.SESSION_COOKIE_NAME)
        if not auth_mod.verify_session_cookie(cookie, APP_AUTH_TOKEN):
            await websocket.close(code=1008)
            return

    if not SAFE_ID_RE.match(run_id or ""):
        await websocket.close(code=1008)
        return

    await websocket.accept()

    if not skip_initial:
        log_path = LOGS_DIR / f"{run_id}.log"
        if log_path.exists():
            try:
                initial_lines = await asyncio.to_thread(_read_log_lossy, log_path)
                if initial_lines:
                    await websocket.send_text(initial_lines)
            except Exception:
                pass

    queue = KaggleService.register_log_subscriber(run_id)

    try:
        run = get_run_by_id(run_id)
        if run:
            KaggleService.ensure_log_stream(run)
    except Exception:
        pass

    try:
        while True:
            try:
                line = await asyncio.wait_for(queue.get(), timeout=1.0)
                await websocket.send_text(line)
            except TimeoutError:
                await websocket.send_text("")
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        KaggleService.unregister_log_subscriber(run_id, queue)
