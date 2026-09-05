"""External kernels browser - lists any notebook owned/visible to an account,
even if it was not launched from this dashboard, and lets the user pull
outputs/logs for it. Uses the same throttled versioned helper as dashboard
runs so 16 accounts don't OOM the server."""

import os
import re
import zipfile
import tempfile
from pathlib import Path
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from config import OUTPUTS_DIR
from services.kaggle_service import KaggleService
from database import get_account_by_username

router = APIRouter(prefix="/api/kernels", tags=["External Kernels"])

SAFE_REF_RE = re.compile(r"^[A-Za-z0-9_\-]+/[A-Za-z0-9_\-]+$")


def _require_account(username: str):
    if not username or not get_account_by_username(username):
        raise HTTPException(status_code=404, detail=f"Account @{username} not found")
    return username


def _parse_ref(ref: str) -> str:
    ref = (ref or "").strip()
    if not SAFE_REF_RE.match(ref):
        raise HTTPException(status_code=400, detail="kernel_ref must be 'owner/slug'")
    return ref


def _ext_run_id(kernel_ref: str) -> str:
    owner, _, slug = kernel_ref.partition("/")
    safe_slug = KaggleService.sanitize_slug(slug)
    safe_owner = (
        "".join(c for c in owner if c.isalnum() or c in ("-", "_")).lower() or "unknown"
    )
    return f"ext_{safe_owner}_{safe_slug}"


@router.get("/list")
async def list_kernels(
    account: str = Query(..., description="Kaggle username whose kernels to list"),
    search: str = Query("", description="Optional search filter"),
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=100),
):
    """Lists kernels visible to account (paginated). Shows title, lastRunTime etc."""
    _require_account(account)
    data = await KaggleService.list_account_kernels(account, search, page, pageSize)
    # Fallback for accounts with wrong stored username (e.g. kaggle_0694f485 hash)
    # If list is empty and username looks like fallback, try discovering real username via CLI/JWT and retry
    if not (data.get("kernels") or []) and account.startswith("kaggle_"):
        try:
            from database import get_account_by_username
            from services.account_manager import AccountManager

            acc = get_account_by_username(account)
            if acc and acc.get("api_key"):
                # Try JWT decode quick
                import base64, json as _json

                key = acc["api_key"]
                real = None
                try:
                    parts = key.strip().split(".")
                    if len(parts) == 3:
                        payload = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
                        claims = _json.loads(base64.urlsafe_b64decode(payload))
                        for f in ["username", "user_name", "sub", "preferred_username"]:
                            if (
                                f in claims
                                and isinstance(claims[f], str)
                                and not claims[f].isdigit()
                            ):
                                real = claims[f]
                                break
                except Exception:
                    pass
                if not real:
                    try:
                        import uuid

                        temp_id = f"debug_{uuid.uuid4().hex[:4]}"
                        real = await AccountManager.fetch_username_for_key(temp_id, key)
                        AccountManager._cleanup_temp_dir(temp_id)
                        if real and real.startswith("kaggle_"):
                            real = None
                    except Exception:
                        pass
                if real and real != account:
                    # Retry list with real username but same credentials (credentials are per stored account id, not username)
                    # We need to call helper with real as user param but credentials still from stored account
                    # Use helper directly with real user but same account's env (via _run_versioned_helper with stored account)
                    # For now, try listing with real username via same account's token
                    retry = await KaggleService.list_account_kernels(
                        account, search, page, pageSize
                    )
                    # Actually retry with real as user param by calling helper with real user but same account credentials
                    # To do that, we need to call helper with real user param but same account's env - our list_account_kernels uses account as both user and credentials
                    # So we call helper directly with real user
                    from services.kaggle_service import KaggleService as KS
                    import json as _j

                    # Direct helper call with real user but credentials of stored account
                    out = await KS._run_versioned_helper(
                        account,
                        ["list", real, search or "", str(page), str(pageSize)],
                        timeout=120,
                    )
                    if out:
                        try:
                            d2 = _j.loads(out)
                            if d2.get("kernels"):
                                data = d2
                        except Exception:
                            pass
        except Exception:
            pass
    # Normalize for frontend: ensure every kernel has ref, title, lastRunTime, currentVersionNumber
    kernels = []
    for k in data.get("kernels") or []:
        kernels.append(
            {
                "ref": k.get("ref") or "",
                "title": k.get("title") or k.get("slug") or "",
                "slug": k.get("slug") or "",
                "author": k.get("author") or "",
                "lastRunTime": k.get("lastRunTime") or k.get("last_run_time") or "",
                "creationTime": k.get("creationTime") or k.get("dateCreated") or "",
                "totalVotes": k.get("totalVotes") or 0,
                "currentVersionNumber": k.get("currentVersionNumber")
                or k.get("current_version_number")
                or None,
                "isPrivate": k.get("isPrivate"),
                "language": k.get("language") or "",
                "kernelType": k.get("kernelType") or "",
                "_raw": k,
            }
        )
    # If still empty and is fallback, include hint for frontend
    hint = None
    if not kernels and account.startswith("kaggle_"):
        hint = f"No notebooks found for @{account} — this looks like a fallback username (real Kaggle username couldn't be resolved when the account was added). The quota shows {account} has GPU usage, so its real username is different. Try re-adding the account with its correct Kaggle username, or check /api/accounts/{account}/debug to see JWT vs CLI discovered username."
    return {
        "success": True,
        "kernels": kernels,
        "nextPageToken": data.get("nextPageToken") or "",
        "page": page,
        "pageSize": pageSize,
        "hint": hint,
    }


@router.get("/status")
async def kernel_status(account: str = Query(...), kernel_ref: str = Query(...)):
    """Live status for any kernel_ref (queued/running/complete/error/stopped)."""
    _require_account(account)
    ref = _parse_ref(kernel_ref)
    resp = await KaggleService.get_kernel_status(account, ref)
    if not resp.get("success"):
        raise HTTPException(
            status_code=502, detail=resp.get("error") or "status lookup failed"
        )
    return {
        "success": True,
        "kernel_ref": ref,
        "status": resp.get("status"),
        "raw": resp.get("raw"),
    }


@router.get("/files")
async def kernel_files(account: str = Query(...), kernel_ref: str = Query(...)):
    """Lists both remote output files (via Kaggle) and local pulled files."""
    _require_account(account)
    ref = _parse_ref(kernel_ref)
    # Remote listing via CLI (throttled inside service)
    remote_files = await KaggleService.list_output_files(account, ref)
    # Local files after pull
    run_id = _ext_run_id(ref)
    target_dir = OUTPUTS_DIR / run_id
    local_files = []
    if target_dir.exists():
        for p in sorted(target_dir.rglob("*")):
            if p.is_file():
                try:
                    rel = str(p.relative_to(target_dir))
                except ValueError:
                    rel = p.name
                local_files.append(
                    {"name": p.name, "rel_path": rel, "size": p.stat().st_size}
                )
    return {
        "success": True,
        "kernel_ref": ref,
        "remote_files": remote_files,
        "local_files": local_files,
        "has_local_download": bool(local_files),
    }


@router.get("/logs")
async def kernel_logs(
    account: str = Query(...),
    kernel_ref: str = Query(...),
    version: Optional[int] = Query(
        None, description="Specific version to fetch log for"
    ),
):
    """Fetches execution logs for any kernel_ref, optionally for a specific version."""
    _require_account(account)
    ref = _parse_ref(kernel_ref)
    if version is not None:
        logs = await KaggleService.fetch_version_log(account, ref, int(version))
        # Fallback to latest logs if version-specific log is empty
        if not logs:
            logs = await KaggleService.fetch_full_logs(account, ref)
        return {"success": True, "kernel_ref": ref, "version": version, "logs": logs}
    logs = await KaggleService.fetch_full_logs(account, ref)
    return {"success": True, "kernel_ref": ref, "logs": logs}


class PullRequest(BaseModel):
    account_username: str
    kernel_ref: str
    version: Optional[int] = None


@router.post("/pull")
async def kernel_pull(payload: PullRequest):
    """Downloads output files for any kernel_ref into data/outputs/ext_<owner>_<slug>/."""
    account = _require_account(payload.account_username)
    ref = _parse_ref(payload.kernel_ref)
    (
        target_dir,
        version_used,
        diagnostics,
    ) = await KaggleService.download_external_outputs(
        account, ref, version=payload.version
    )
    if not target_dir or not target_dir.exists():
        raise HTTPException(
            status_code=502,
            detail=" | ".join(diagnostics)
            if diagnostics
            else "No output files found for this kernel (it may still be running or published no files).",
        )
    files = []
    for p in sorted(target_dir.rglob("*")):
        if p.is_file():
            try:
                rel = str(p.relative_to(target_dir))
            except ValueError:
                rel = p.name
            files.append(
                {"name": p.name, "rel_path": rel, "size": p.stat().st_size, "path": rel}
            )
    if not files:
        raise HTTPException(
            status_code=502,
            detail="Pull succeeded but no files were saved (log-only or empty).",
        )
    msg = f"Downloaded {len(files)} file(s) from Kaggle"
    if version_used:
        msg += f" (version {version_used} snapshot)"
    elif payload.version:
        msg += f" (version {payload.version} snapshot)"
    msg += "."
    return {
        "success": True,
        "message": msg,
        "files": files,
        "version_used": version_used or payload.version,
        "kernel_ref": ref,
    }


@router.get("/versions")
async def kernel_versions(
    account: str = Query(...),
    kernel_ref: str = Query(...),
    max_versions: int = Query(20, ge=1, le=50),
):
    """Lists per-version snapshots for a kernel, newest first. Each entry has version, creationTime, status, fileCount."""
    _require_account(account)
    ref = _parse_ref(kernel_ref)
    data = await KaggleService.list_kernel_versions(account, ref, max_versions)
    return {"success": True, "kernel_ref": ref, **data}


@router.get("/debug/current_version")
async def debug_current_version(
    account: str = Query(...), kernel_ref: str = Query(...)
):
    """Debug helper: shows what current_version logic returns and raw GetKernel/ListKernels probes."""
    _require_account(account)
    ref = _parse_ref(kernel_ref)
    owner, _, slug = ref.partition("/")
    # Try current_version via service
    cur = await KaggleService.get_kernel_current_version(account, ref)
    # Also try direct helper list_kernels raw for debugging
    raw_list = await KaggleService.list_account_kernels(account, slug, 1, 5)
    return {
        "success": True,
        "kernel_ref": ref,
        "current_version_via_service": cur,
        "list_raw_sample": (raw_list.get("kernels") or [])[:2],
    }


class StopRequest(BaseModel):
    account_username: str
    kernel_ref: str
    title: Optional[str] = None


@router.post("/stop")
async def kernel_stop(payload: StopRequest):
    """Stops any running kernel (even not in DB) by pushing a cancel stub."""
    account = _require_account(payload.account_username)
    ref = _parse_ref(payload.kernel_ref)
    # Check live status first - don't push stub if already terminal
    status_resp = await KaggleService.get_kernel_status(account, ref)
    st = (status_resp.get("status") or "unknown").lower()
    if st in ("complete", "error", "stopped"):
        return {
            "success": False,
            "error": f"Kernel already {st} - no stop needed",
            "status": st,
        }
    result = await KaggleService.stop_external_kernel(account, ref, title=payload.title)
    if result.get("success"):
        return {"success": True, "message": result.get("message"), "kernel_ref": ref}
    raise HTTPException(status_code=502, detail=result.get("error") or "Stop failed")


@router.get("/files/download/{filename:path}")
async def download_single_file(
    account: str = Query(...), kernel_ref: str = Query(...), filename: str = ""
):
    """Downloads a single pulled file for an external kernel."""
    _require_account(account)
    ref = _parse_ref(kernel_ref)
    run_id = _ext_run_id(ref)
    target_dir = OUTPUTS_DIR / run_id
    # Prevent path traversal
    safe_name = Path(filename).name
    if not safe_name or safe_name != filename.split("/")[-1]:
        # Allow subfolder but sanitize
        candidate = (target_dir / filename).resolve()
        try:
            candidate.relative_to(target_dir.resolve())
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid filename")
        if not candidate.exists() or not candidate.is_file():
            raise HTTPException(status_code=404, detail="File not found")
        return FileResponse(path=str(candidate), filename=candidate.name)
    # Flat file case
    candidate = target_dir / safe_name
    if not candidate.exists():
        # Try recursive find
        candidates = list(target_dir.rglob(safe_name))
        if not candidates:
            raise HTTPException(status_code=404, detail="File not found")
        candidate = candidates[0]
    return FileResponse(path=str(candidate), filename=candidate.name)


@router.get("/files/download-zip")
async def download_zip(account: str = Query(...), kernel_ref: str = Query(...)):
    """Zips all locally pulled files for an external kernel."""
    _require_account(account)
    ref = _parse_ref(kernel_ref)
    run_id = _ext_run_id(ref)
    target_dir = OUTPUTS_DIR / run_id
    if not target_dir.exists() or not any(target_dir.rglob("*")):
        raise HTTPException(
            status_code=404, detail="No local files to zip - pull first"
        )
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    tmp.close()
    with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in target_dir.rglob("*"):
            if p.is_file():
                zf.write(p, arcname=str(p.relative_to(target_dir)))
    safe_slug = KaggleService.sanitize_slug(ref.split("/", 1)[1])
    return FileResponse(
        path=tmp.name, filename=f"{safe_slug}_outputs.zip", media_type="application/zip"
    )
