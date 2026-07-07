import os
import re
import zipfile
import tempfile
from pathlib import Path
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from starlette.background import BackgroundTask
from app.services.kaggle_service import KaggleService
from app.database import get_run_by_id
from app.config import OUTPUTS_DIR

router = APIRouter(prefix="/api/runs", tags=["Output Files"])

SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")

def _validate_run_id(run_id: str) -> str:
    """Rejects run ids that could escape the outputs directory."""
    if not SAFE_ID_RE.match(run_id or ""):
        raise HTTPException(status_code=400, detail="Invalid run id")
    return run_id

def _safe_output_path(run_id: str, filename: str) -> Path:
    """Resolves a user-supplied filename inside the run's output dir, blocking traversal."""
    _validate_run_id(run_id)
    if not filename or "\x00" in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    base = (OUTPUTS_DIR / run_id).resolve()
    target = (base / filename).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        raise HTTPException(status_code=400, detail="Path traversal blocked")
    return target

@router.get("/{run_id}/files")
async def list_files(run_id: str):
    run = get_run_by_id(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    files = await KaggleService.list_output_files(run["account_username"], run["kernel_ref"])

    # Check if files have been downloaded locally
    local_output_dir = OUTPUTS_DIR / run_id
    local_files = []
    if local_output_dir.exists():
        for f in local_output_dir.rglob("*"):
            if f.is_file():
                local_files.append({
                    "name": f.name,
                    "size": f.stat().st_size,
                    "rel_path": str(f.relative_to(local_output_dir))
                })

    return {
        "success": True,
        "remote_files": files,
        "local_files": local_files,
        "has_local_download": local_output_dir.exists() and len(local_files) > 0
    }

@router.post("/{run_id}/files/pull")
async def pull_files_from_kaggle(run_id: str):
    run = get_run_by_id(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    try:
        target_dir = await KaggleService.download_outputs(run["account_username"], run["kernel_ref"], run_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Kaggle output download failed: {e}")

    downloaded = []
    for f in target_dir.rglob("*"):
        if f.is_file():
            downloaded.append({
                "name": f.name,
                "size": f.stat().st_size,
                "path": str(f.relative_to(target_dir))
            })

    return {
        "success": True,
        "message": f"Downloaded {len(downloaded)} files from Kaggle.",
        "files": downloaded
    }

@router.get("/{run_id}/files/download/{filename:path}")
async def download_single_file(run_id: str, filename: str):
    local_file = _safe_output_path(run_id, filename)
    if not local_file.exists():
        # Try pulling first (only for known runs; never auto-pull on traversal attempts)
        run = get_run_by_id(_validate_run_id(run_id))
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        try:
            await KaggleService.download_outputs(run["account_username"], run["kernel_ref"], run_id)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Kaggle output download failed: {e}")

        if not local_file.exists():
            raise HTTPException(status_code=404, detail="File not found on server")

    return FileResponse(path=str(local_file), filename=local_file.name)

@router.get("/{run_id}/files/download-zip")
async def download_all_zip(run_id: str):
    run = get_run_by_id(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    _validate_run_id(run_id)

    target_dir = OUTPUTS_DIR / run_id
    if not target_dir.exists() or not any(target_dir.iterdir()):
        try:
            await KaggleService.download_outputs(run["account_username"], run["kernel_ref"], run_id)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Kaggle output download failed: {e}")

    if not target_dir.exists() or not any(target_dir.iterdir()):
        raise HTTPException(status_code=404, detail="No output files available to download")

    # Stream from a temp file instead of buffering the whole archive in RAM
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zip_file:
            for root, dirs, files in os.walk(target_dir):
                for file in files:
                    file_path = Path(root) / file
                    archive_name = file_path.relative_to(target_dir)
                    zip_file.write(file_path, archive_name)
        tmp.close()
    except Exception:
        tmp.close()
        os.unlink(tmp.name)
        raise

    safe_slug = re.sub(r"[^A-Za-z0-9_\-]", "_", run["kernel_slug"])
    return FileResponse(
        path=tmp.name,
        media_type="application/zip",
        filename=f"{safe_slug}_outputs.zip",
        background=BackgroundTask(os.unlink, tmp.name)
    )
