import asyncio
import csv
import functools
import io
import logging
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from config import OUTPUTS_DIR, get_kaggle_cli_path

from services.account_manager import AccountManager
from services.kaggle_kernel_identity import sanitize_slug, strip_kaggle_cli_warnings

logger = logging.getLogger("kaggle_outputs")

VERSIONED_FETCH_TIMEOUT_SECONDS = int(
    os.getenv("VERSIONED_FETCH_TIMEOUT_SECONDS", "600")
)
OUTPUT_PULL_TIMEOUT_SECONDS = int(os.getenv("OUTPUT_PULL_TIMEOUT_SECONDS", "300"))
OUTPUT_LIST_TIMEOUT_SECONDS = int(os.getenv("OUTPUT_LIST_TIMEOUT_SECONDS", "60"))

VERSIONED_CONCURRENCY = max(1, int(os.getenv("VERSIONED_CONCURRENCY", "3")))
_versioned_pool: ThreadPoolExecutor | None = None


def _versioned_executor() -> ThreadPoolExecutor:
    global _versioned_pool
    if _versioned_pool is None:
        _versioned_pool = ThreadPoolExecutor(
            max_workers=VERSIONED_CONCURRENCY,
            thread_name_prefix="kaggle-versioned",
        )
    return _versioned_pool


async def list_output_files(
    account_username: str, kernel_ref: str
) -> list[dict[str, Any]]:
    from services.kaggle_service import KaggleService

    cli = get_kaggle_cli_path()
    cmd = [cli, "kernels", "files", "-v", kernel_ref]
    env = AccountManager.get_account_env(account_username)
    proc = None
    try:
        async with KaggleService._get_kernel_status_semaphore():
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout, _stderr = await asyncio.wait_for(
                proc.communicate(), timeout=OUTPUT_LIST_TIMEOUT_SECONDS
            )
        out_str = strip_kaggle_cli_warnings(stdout.decode("utf-8", errors="ignore"))

        files = []
        if out_str and out_str.strip():
            if out_str.strip().lower() in ("no files found", "no files"):
                return []
            try:
                reader = csv.DictReader(io.StringIO(out_str))
            except Exception:
                return []
            if not reader.fieldnames:
                return []
            lowered = {(h or "").strip().lower() for h in reader.fieldnames}
            if not lowered & {"name", "filename", "file_name"}:
                return []
            for row in reader:
                if not isinstance(row, dict):
                    continue
                norm = {(k or "").strip().lower(): v for k, v in row.items()}
                name = str(
                    norm.get("name")
                    or norm.get("filename")
                    or norm.get("file_name")
                    or ""
                ).strip()
                if not name:
                    continue
                size = norm.get("size", "")
                files.append(
                    {
                        "name": name,
                        "size": size if size not in (None, "") else "N/A",
                        "creationDate": norm.get("creationdate", ""),
                    }
                )
        return files
    except TimeoutError:
        logger.warning(f"Listing output files timed out for {kernel_ref}")
        return []
    except Exception as e:
        logger.error(f"Failed to list output files for {kernel_ref}: {e}")
        return []
    finally:
        if proc is not None and getattr(proc, "returncode", None) is None:
            if hasattr(proc, "kill"):
                try:
                    proc.kill()
                except Exception:
                    pass
            if hasattr(proc, "wait"):
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except Exception:
                    pass


async def _run_vo(
    account_username: str,
    fn: Callable[..., Any],
    *args: Any,
    timeout: int = VERSIONED_FETCH_TIMEOUT_SECONDS,
) -> Any:
    env = AccountManager.get_account_env(account_username)
    token = (env.get("KAGGLE_API_TOKEN") or "").strip()
    if not token:
        logger.warning(
            "Versioned output call skipped for @%s: no KAGGLE_API_TOKEN "
            "(kaggle.json credentials cannot authenticate these routes). "
            "Falling back to the plain latest pull.",
            account_username,
        )
        return None
    try:
        loop = asyncio.get_running_loop()
        return await asyncio.wait_for(
            loop.run_in_executor(
                _versioned_executor(), functools.partial(fn, *args, token=token)
            ),
            timeout=timeout,
        )
    except TimeoutError:
        logger.warning(
            "Versioned-output call timed out for @%s (%s)",
            account_username,
            getattr(fn, "__name__", str(fn)),
        )
        return None
    except Exception as e:
        logger.warning(f"Versioned-output call failed: {e}")
        return None


async def list_account_kernels(
    account_username: str,
    search: str = "",
    page: int = 1,
    page_size: int = 20,
    owner: str | None = None,
) -> dict[str, Any]:
    from services import kaggle_versioned_output as _vo

    page = max(1, int(page or 1))
    page_size = max(1, min(100, int(page_size or 20)))
    res = await _run_vo(
        account_username,
        _vo.list_kernels,
        owner or account_username,
        search or "",
        page,
        page_size,
        timeout=120,
    )
    if not res:
        return {"kernels": [], "nextPageToken": ""}
    kernels_raw, next_token = res
    kernels = list(kernels_raw or [])

    def _time_key(k):
        return (
            k.get("lastRunTime")
            or k.get("last_run_time")
            or k.get("creationTime")
            or ""
        )

    try:
        kernels.sort(key=_time_key, reverse=True)
    except Exception:
        pass
    return {"kernels": kernels, "nextPageToken": next_token or ""}


async def list_kernel_versions(
    account_username: str, kernel_ref: str, max_versions: int = 20
) -> dict[str, Any]:
    from services import kaggle_versioned_output as _vo

    owner, _, slug = kernel_ref.partition("/")
    if not owner or not slug:
        return {"versions": [], "current_version": None}
    vers = await _run_vo(
        account_username, _vo.list_versions, owner, slug, max_versions, timeout=180
    )
    if not vers:
        return {"versions": [], "current_version": None}
    return {
        "versions": vers,
        "current_version": vers[0]["version"] if vers else None,
    }


async def fetch_version_log(
    account_username: str, kernel_ref: str, version: int
) -> str:
    from services import kaggle_versioned_output as _vo

    owner, _, slug = kernel_ref.partition("/")
    if not owner or not slug:
        return ""
    log = await _run_vo(
        account_username, _vo.fetch_version_log, owner, slug, int(version), timeout=60
    )
    return log or ""


async def get_kernel_current_version(
    account_username: str, kernel_ref: str
) -> int | None:
    from services import kaggle_versioned_output as _vo

    owner, _, slug = kernel_ref.partition("/")
    if not owner or not slug:
        return None
    v = await _run_vo(account_username, _vo.current_version, owner, slug, timeout=120)
    return int(v) if v else None


async def download_outputs_of_version(
    account_username: str, kernel_ref: str, version: int, run_id: str
) -> Path | None:
    from services import kaggle_versioned_output as _vo

    owner, _, slug = kernel_ref.partition("/")
    if not owner or not slug:
        return None
    target_dir = OUTPUTS_DIR / str(run_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    res = await _run_vo(
        account_username,
        _vo.fetch_version_output,
        owner,
        slug,
        int(version),
        str(target_dir),
    )
    if not res:
        return None
    saved, notes = res
    if notes:
        logger.info(
            f"Version {version} output probe for {run_id}: " + "; ".join(notes)
        )
    if not saved:
        return None
    logger.info(f"Version {version} output for {run_id}: saved {len(saved)} file(s).")
    return target_dir


async def download_latest_outputs(
    account_username: str,
    kernel_ref: str,
    run_id: str,
    prefer_version: int | None = None,
    probe_previous_for_stop: bool = False,
):
    diagnostics: list[str] = []
    attempts: list[int] = []
    if prefer_version:
        attempts.append(int(prefer_version))
    else:
        current = await get_kernel_current_version(account_username, kernel_ref)
        if not current:
            diagnostics.append(
                "meta: get_kernel_current_version returned None "
                "(auth failure, network error, or kernel list unavailable)"
            )
        else:
            if probe_previous_for_stop and current > 1:
                attempts.append(current - 1)
            attempts.append(current)

    for v in attempts:
        try:
            got = await download_outputs_of_version(
                account_username, kernel_ref, v, run_id
            )
        except Exception as e:
            diagnostics.append(
                f"version {v}: download_outputs_of_version raised: {e}"
            )
            continue
        if got:
            return got, v, diagnostics
        diagnostics.append(
            f"version {v}: no files published (log-only or empty for every "
            f"versionLabel tried - helper notes were logged at INFO level)"
        )

    try:
        plain = await download_outputs(account_username, kernel_ref, run_id)
        return plain, None, diagnostics
    except Exception as e:
        diagnostics.append(f"plain pull failed: {e}")
        return None, None, diagnostics


async def download_external_outputs(
    account_username: str, kernel_ref: str, version: int | None = None
):
    owner, _, slug = kernel_ref.partition("/")
    if not owner or not slug:
        return None, None, ["invalid kernel_ref"]
    safe_slug = sanitize_slug(slug)
    safe_owner = (
        "".join(c for c in owner if c.isalnum() or c in ("-", "_")).lower()
        or "unknown"
    )
    run_id = f"ext_{safe_owner}_{safe_slug}"
    if version:
        got = await download_outputs_of_version(
            account_username, kernel_ref, int(version), run_id
        )
        if got:
            return got, int(version), []
        try:
            cur = await get_kernel_current_version(account_username, kernel_ref)
            if cur is None or int(version) == cur:
                plain, _, diag = await download_latest_outputs(
                    account_username, kernel_ref, run_id
                )
                if plain:
                    return (
                        plain,
                        int(version),
                        [
                            f"version {version}: versioned probe fell back to latest pull"
                        ]
                        + diag,
                    )
        except Exception:
            pass
        return (
            None,
            None,
            [
                f"version {version}: no files published or recoverable for this specific version snapshot"
            ],
        )
    return await download_latest_outputs(account_username, kernel_ref, run_id)


async def download_outputs(account_username: str, kernel_ref: str, run_id: str) -> Path:
    from services import kaggle_service as _ksmod

    timeout = _ksmod.OUTPUT_PULL_TIMEOUT_SECONDS
    target_dir = OUTPUTS_DIR / run_id
    target_dir.mkdir(parents=True, exist_ok=True)

    env = AccountManager.get_account_env(account_username)
    token = (env.get("KAGGLE_API_TOKEN") or "").strip()
    if token and "/" in kernel_ref:
        owner, _, slug = kernel_ref.partition("/")
        if owner and slug:
            from services import kaggle_versioned_output as _vo

            def _stream_latest() -> list[str]:
                import httpx

                saved: list[str] = []
                with httpx.Client(
                    timeout=_vo.TIMEOUT,
                    headers={"Authorization": f"Bearer {token}"},
                    follow_redirects=True,
                ) as client:
                    page_token = ""
                    page_log = ""
                    while True:
                        data = _vo._list_output_page(
                            client, owner, slug, label="", page_token=page_token
                        )
                        files = data.get("files") or []
                        if files:
                            saved.extend(
                                _vo._download_files(client, files, str(target_dir))
                            )
                        if data.get("log"):
                            page_log = data["log"]
                        page_token = data.get("nextPageToken") or ""
                        if not page_token:
                            break
                    if page_log:
                        log_path = target_dir / f"{slug}.log"
                        log_path.write_text(page_log, encoding="utf-8")
                return saved

            try:
                loop = asyncio.get_running_loop()
                await asyncio.wait_for(
                    loop.run_in_executor(_versioned_executor(), _stream_latest),
                    timeout=timeout,
                )
                return target_dir
            except TimeoutError:
                raise RuntimeError(f"kaggle output stream timed out after {timeout}s")
            except Exception as e:
                logger.warning(
                    "Streaming latest output failed for %s (%s); falling back to CLI",
                    kernel_ref,
                    e,
                )

    cli = get_kaggle_cli_path()
    cmd = [cli, "kernels", "output", kernel_ref, "-p", str(target_dir), "-o"]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        raise RuntimeError(f"kaggle kernels output timed out after {timeout}s")
    finally:
        if getattr(proc, "returncode", None) is None:
            if hasattr(proc, "kill"):
                try:
                    proc.kill()
                except Exception:
                    pass
            if hasattr(proc, "wait"):
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except Exception:
                    pass
    if proc.returncode != 0:
        err_tail = strip_kaggle_cli_warnings(
            stderr.decode("utf-8", errors="ignore")
        ).strip()[-500:]
        raise RuntimeError(
            f"kaggle kernels output failed (rc={proc.returncode}): {err_tail}"
        )
    return target_dir
