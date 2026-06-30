import os
import zipfile
import io
from pathlib import Path
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from app.services.kaggle_service import KaggleService
from app.database import get_run_by_id
from app.config import OUTPUTS_DIR

router = APIRouter(prefix="/api/runs", tags=["Output Files"])

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

    target_dir = await KaggleService.download_outputs(run["account_username"], run["kernel_ref"], run_id)
    
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
    local_file = OUTPUTS_DIR / run_id / filename
    if not local_file.exists():
        # Try pulling first
        run = get_run_by_id(run_id)
        if run:
            await KaggleService.download_outputs(run["account_username"], run["kernel_ref"], run_id)
        
        if not local_file.exists():
            raise HTTPException(status_code=404, detail="File not found on server")

    return FileResponse(path=str(local_file), filename=local_file.name)

@router.get("/{run_id}/files/download-zip")
async def download_all_zip(run_id: str):
    run = get_run_by_id(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    target_dir = OUTPUTS_DIR / run_id
    if not target_dir.exists() or not any(target_dir.iterdir()):
        await KaggleService.download_outputs(run["account_username"], run["kernel_ref"], run_id)

    if not target_dir.exists() or not any(target_dir.iterdir()):
        raise HTTPException(status_code=404, detail="No output files available to download")

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for root, dirs, files in os.walk(target_dir):
            for file in files:
                file_path = Path(root) / file
                archive_name = file_path.relative_to(target_dir)
                zip_file.write(file_path, archive_name)

    zip_buffer.seek(0)
    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={run['kernel_slug']}_outputs.zip"}
    )
