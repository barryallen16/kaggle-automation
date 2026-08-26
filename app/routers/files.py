import os
import re
import shutil
import zipfile
import tempfile
from pathlib import Path
from typing import List
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
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
        "message": f"Downloaded {len(downloaded)} file(s) from Kaggle. Files snapshotted before a stop are kept.",
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

# ------------------------------------------------------------------
# Local output deletion (keeps remote Kaggle artifacts untouched)
# ------------------------------------------------------------------
@router.delete("/{run_id}/files/{filename:path}")
async def delete_single_output_file(run_id: str, filename: str):
    """Deletes one downloaded output file from the server's local storage."""
    target = _safe_output_path(run_id, filename)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="File not found on server")
    try:
        target.unlink()
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Could not delete file: {e}")
    return {"success": True, "message": f"Deleted {filename} from local outputs."}

@router.delete("/{run_id}/files")
async def delete_all_output_files(run_id: str):
    """Deletes the entire local output folder for a run (Kaggle copy untouched)."""
    _validate_run_id(run_id)
    run = get_run_by_id(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    target_dir = OUTPUTS_DIR / run_id
    if not target_dir.exists():
        raise HTTPException(status_code=404, detail="No local output files stored for this run")
    shutil.rmtree(target_dir, ignore_errors=True)
    return {"success": True, "message": f"Deleted all local output files for run {run_id}."}

# ------------------------------------------------------------------
# Cross-run merge: pick individual files from many finished notebooks
# and download them concatenated into one file.
# ------------------------------------------------------------------
class MergeItem(BaseModel):
    run_id: str
    filename: str

class MergeRequest(BaseModel):
    items: List[MergeItem]

@router.post("/files/merge-download")
async def merge_selected_files(request: MergeRequest):
    """Cat-style merge: raw byte concatenation of selected files in cart order.

    Exactly `cat shard/* > merged.jsonl` - no parsing, no dedupe, no
    re-encoding, so it is instant even for hundreds of MB of shards.
    The output extension follows the most common suffix among inputs.
    """
    if not request.items:
        raise HTTPException(status_code=400, detail="Cart is empty - select at least one file.")

    # 1. Resolve every selected file locally; auto-pull missing runs from Kaggle.
    resolved: List[Path] = []
    seen_runs: set = set()
    for item in request.items:
        path = _safe_output_path(item.run_id, item.filename)
        if not path.is_file() and item.run_id not in seen_runs:
            seen_runs.add(item.run_id)
            run = get_run_by_id(_validate_run_id(item.run_id))
            if not run:
                raise HTTPException(status_code=404, detail=f"Run {item.run_id} not found")
            try:
                await KaggleService.download_outputs(run["account_username"], run["kernel_ref"], item.run_id)
            except Exception as e:
                raise HTTPException(status_code=502, detail=f"Output pull for run {item.run_id} failed: {e}")
        if not path.is_file():
            raise HTTPException(status_code=404, detail=f"File '{item.filename}' not found in run {item.run_id}")
        resolved.append(path)

    # 2. Output name: most common input suffix (sanitized), else .bin
    suffixes = [p.suffix.lower() for p in resolved if p.suffix]
    ext = max(set(suffixes), key=suffixes.count) if suffixes else ".bin"
    if not re.fullmatch(r"\.[A-Za-z0-9_]{1,10}", ext):
        ext = ".bin"

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
    tmp.close()
    try:
        with open(tmp.name, "wb") as out:
            for p in resolved:
                with open(p, "rb") as src:
                    shutil.copyfileobj(src, out, length=1024 * 1024)
    except Exception:
        os.unlink(tmp.name)
        raise

    media_type = "application/octet-stream"
    if ext in (".jsonl", ".ndjson"):
        media_type = "application/x-ndjson"
    elif ext in (".txt", ".csv", ".log"):
        media_type = "text/plain"

    return FileResponse(
        path=tmp.name,
        filename=f"merged{ext}",
        media_type=media_type,
        background=BackgroundTask(os.unlink, tmp.name)
    )
