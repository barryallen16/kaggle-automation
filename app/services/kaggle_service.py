import asyncio
import json
import logging
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from config import (
    KAGGLE_MAX_GPU_SESSIONS_PER_ACCOUNT,
    LOGS_DIR,
    NOTEBOOKS_DIR,
    get_kaggle_cli_path,
    get_kernel_env_defaults,
)
from database import (
    create_run_record,
    get_active_runs,
    get_run_by_id,
    set_run_output_version,
    update_run_status,
    utcnow_iso,
)

from services import kaggle_logs, kaggle_outputs, kaggle_status
from services.account_manager import AccountManager
from services.kaggle_kernel_identity import (
    ACCELERATOR_MAP as _ACCELERATOR_MAP,
)
from services.kaggle_kernel_identity import (
    DEFAULT_KERNELSPEC as _DEFAULT_KERNELSPEC,
)
from services.kaggle_kernel_identity import (
    build_env_preamble as _build_env_preamble,
)
from services.kaggle_kernel_identity import (
    ensure_executable_notebook as _ensure_executable_notebook,
)
from services.kaggle_kernel_identity import (
    normalize_kernel_status as _normalize_kernel_status_fn,
)
from services.kaggle_kernel_identity import (
    resolve_accelerator as _resolve_accelerator,
)
from services.kaggle_kernel_identity import (
    sanitize_slug as _sanitize_slug,
)
from services.kaggle_kernel_identity import (
    strip_kaggle_cli_warnings,
)
from services.ops_tracker import run_stop_key, tracker

logger = logging.getLogger("kaggle_service")

VERSIONED_FETCH_TIMEOUT_SECONDS = kaggle_outputs.VERSIONED_FETCH_TIMEOUT_SECONDS
OUTPUT_PULL_TIMEOUT_SECONDS = kaggle_outputs.OUTPUT_PULL_TIMEOUT_SECONDS


def _write_json_file(path: Path, payload: object) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def _write_text_file(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _append_text_file(path: Path, text: str) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(text)


def _classify_push_failure(combined: str) -> str:
    low = combined.lower()
    if "maximum batch gpu session count" in low:
        return "session_cap"
    if "409" in combined or "conflict" in low:
        return "conflict"
    return ""


class KaggleService:
    STOP_CAPTURE_RETRY_DELAYS = (6, 12, 20)
    SESSION_CAP_RETRY_DELAYS = (20,)
    CONFLICT_RETRY_DELAYS = (5, 10, 20, 40)
    _active_stream_processes: ClassVar[dict[str, asyncio.subprocess.Process]] = (
        kaggle_logs._active_stream_processes
    )
    _active_stream_tasks: ClassVar[dict[str, asyncio.Task]] = (
        kaggle_logs._active_stream_tasks
    )
    _log_subscribers: ClassVar[dict[str, list[asyncio.Queue]]] = (
        kaggle_logs._log_subscribers
    )

    PUSH_CONCURRENCY_LIMIT = max(1, int(os.getenv("PUSH_CONCURRENCY", "3")))
    KERNEL_STATUS_CONCURRENCY_LIMIT = max(
        1, int(os.getenv("KERNEL_STATUS_CONCURRENCY", "3"))
    )
    KERNEL_STATUS_TIMEOUT_SECONDS = kaggle_status.KERNEL_STATUS_TIMEOUT_SECONDS
    STOP_CONCURRENCY_LIMIT = max(1, int(os.getenv("STOP_CONCURRENCY", "3")))
    _semaphores: ClassVar[dict[str, tuple[int, asyncio.Semaphore]]] = {}
    _account_locks: ClassVar[dict[str, tuple[int, asyncio.Lock]]] = {}

    @classmethod
    def _get_account_lock(cls, account_username: str) -> asyncio.Lock:
        loop_id = id(asyncio.get_running_loop())
        key = AccountManager.resolve_effective_username(account_username) or "_default"
        cached = cls._account_locks.get(key)
        if cached is None or cached[0] != loop_id:
            cached = (loop_id, asyncio.Lock())
            cls._account_locks[key] = cached
        return cached[1]

    @classmethod
    def _get_semaphore(cls, name: str, limit: int) -> asyncio.Semaphore:
        loop_id = id(asyncio.get_running_loop())
        cached = cls._semaphores.get(name)
        if cached is None or cached[0] != loop_id:
            cached = (loop_id, asyncio.Semaphore(limit))
            cls._semaphores[name] = cached
        return cached[1]

    @classmethod
    def _get_push_semaphore(cls) -> asyncio.Semaphore:
        return cls._get_semaphore("push", cls.PUSH_CONCURRENCY_LIMIT)

    @classmethod
    def _get_kernel_status_semaphore(cls) -> asyncio.Semaphore:
        return cls._get_semaphore("status", cls.KERNEL_STATUS_CONCURRENCY_LIMIT)

    @classmethod
    def _get_stop_semaphore(cls) -> asyncio.Semaphore:
        return cls._get_semaphore("stop", cls.STOP_CONCURRENCY_LIMIT)

    ACCELERATOR_MAP: ClassVar[dict[str, str]] = _ACCELERATOR_MAP

    @classmethod
    def build_env_preamble(cls, env_vars: dict[str, str], is_notebook: bool) -> str:
        return _build_env_preamble(env_vars, is_notebook)

    @classmethod
    def resolve_accelerator(cls, accelerator: str) -> str:
        return _resolve_accelerator(accelerator)

    @classmethod
    def sanitize_slug(cls, title: str) -> str:
        return _sanitize_slug(title)

    DEFAULT_KERNELSPEC: ClassVar[dict[str, str]] = _DEFAULT_KERNELSPEC

    @classmethod
    def ensure_executable_notebook(cls, code_content: str) -> str:
        return _ensure_executable_notebook(code_content)

    @classmethod
    def _resolve_free_title(
        cls,
        account_username: str,
        title: str,
        active_refs: set[str] | None = None,
        max_attempts: int = 10,
    ) -> str | None:
        if active_refs is None:
            active_refs = {r["kernel_ref"] for r in get_active_runs()}
        base = (title or "")[:50]
        if f"{account_username}/{cls.sanitize_slug(base)}" not in active_refs:
            return base
        for n in range(2, max_attempts + 2):
            suffix = f" ({n})"
            cand = base[: max(0, 50 - len(suffix))] + suffix
            if f"{account_username}/{cls.sanitize_slug(cand)}" not in active_refs:
                return cand
        return None

    @classmethod
    async def push_kernel(
        cls,
        account_username: str,
        title: str,
        code_content: str,
        filename: str = "notebook.ipynb",
        accelerator: str = "none",
        enable_internet: bool = True,
        is_trial: bool = False,
        timeout_seconds: int | None = None,
        env_vars: dict[str, str] | None = None,
        workload_id: str | None = None,
        shard_index: int | None = None,
        total_shards: int | None = None,
    ) -> dict[str, Any]:
        account_username = AccountManager.resolve_effective_username(account_username)
        run_hash = uuid.uuid4().hex[:6]
        run_id = f"run_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}_{run_hash}"
        requested_title = title[:50]
        clean_title = cls._resolve_free_title(account_username, title)
        if clean_title is None:
            return {
                "success": False,
                "status": "conflict",
                "error": (
                    f"'{requested_title}' and its numbered copies are all running "
                    f"on this account. Stop one first, wait for it to finish, "
                    "or use a different title."
                ),
                "conflict_run_id": None,
                "run_id": None,
            }
        renamed = clean_title != requested_title
        slug = cls.sanitize_slug(clean_title)
        kernel_ref = f"{account_username}/{slug}"
        kaggle_url = f"https://www.kaggle.com/code/{kernel_ref}"

        run_dir = NOTEBOOKS_DIR / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        is_notebook = filename.endswith(".ipynb")
        kernel_type = "notebook" if is_notebook else "script"

        effective_env = {**get_kernel_env_defaults(), **(env_vars or {})}
        preamble = cls.build_env_preamble(effective_env, is_notebook)

        if is_notebook:
            code_content = cls.ensure_executable_notebook(code_content)
            if preamble:
                nb = json.loads(code_content)
                env_nb = json.loads(preamble)
                nb["cells"] = env_nb["cells"] + nb["cells"]
                code_content = json.dumps(nb)
        elif preamble:
            code_content = preamble + code_content

        code_path = run_dir / filename
        await asyncio.to_thread(_write_text_file, code_path, code_content)

        resolved_machine = cls.resolve_accelerator(accelerator)
        machine_lower = (resolved_machine or accelerator).lower()
        enable_gpu = (
            "true"
            if (
                "gpu" in machine_lower
                or "nvidia" in machine_lower
                or "t4" in machine_lower
            )
            else "false"
        )
        enable_tpu = "true" if "tpu" in machine_lower else "false"

        metadata = {
            "id": kernel_ref,
            "title": clean_title,
            "code_file": filename,
            "language": "python",
            "kernel_type": kernel_type,
            "is_private": "true",
            "enable_gpu": enable_gpu,
            "enable_tpu": enable_tpu,
            "enable_internet": "true" if enable_internet else "false",
            "machine_shape": resolved_machine or "",
            "dataset_sources": [],
            "competition_sources": [],
            "kernel_sources": [],
            "model_sources": [],
        }

        meta_path = run_dir / "kernel-metadata.json"
        await asyncio.to_thread(_write_json_file, meta_path, metadata)

        cli = get_kaggle_cli_path()
        cmd = [cli, "kernels", "push", "-p", str(run_dir)]

        resolved_acc = cls.resolve_accelerator(accelerator)
        if resolved_acc:
            cmd.extend(["--accelerator", resolved_acc])

        effective_timeout = timeout_seconds or (300 if is_trial else 43200)
        if effective_timeout:
            cmd.extend(["-t", str(effective_timeout)])

        log_file_path = LOGS_DIR / f"{run_id}.log"
        log_file_path.parent.mkdir(parents=True, exist_ok=True)
        header = (
            f"=== Kaggle Run Initialized: {utcnow_iso()} ===\n"
            f"Kernel: {kernel_ref}\n"
            f"Title: {title}\n"
            f"Accelerator: {accelerator}\n"
            f"Trial Mode: {is_trial} (Timeout: {effective_timeout}s)\n"
            f"Command: {' '.join(cmd)}\n\n"
        )
        await asyncio.to_thread(_write_text_file, log_file_path, header)

        env = AccountManager.get_account_env(account_username)

        cap_delays = cls.SESSION_CAP_RETRY_DELAYS
        conflict_delays = cls.CONFLICT_RETRY_DELAYS
        total_attempts = max(len(cap_delays), len(conflict_delays)) + 1

        out_str = ""
        err_str = ""
        proc = None

        try:
            for attempt in range(total_attempts):
                async with cls._get_push_semaphore():
                    proc = await asyncio.create_subprocess_exec(
                        *cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        env=env,
                    )
                    try:
                        stdout, stderr = await asyncio.wait_for(
                            proc.communicate(), timeout=120
                        )
                    except TimeoutError:
                        if proc.returncode is None:
                            try:
                                proc.kill()
                            except Exception:
                                pass
                            try:
                                await asyncio.wait_for(proc.wait(), timeout=5)
                            except Exception:
                                pass
                        raise
                out_str = strip_kaggle_cli_warnings(
                    stdout.decode("utf-8", errors="ignore")
                )
                err_str = strip_kaggle_cli_warnings(
                    stderr.decode("utf-8", errors="ignore")
                )

                combined_out = out_str + "\n" + err_str
                low = combined_out.lower()
                is_push_err = proc.returncode != 0 or "kernel push error" in low
                reason = _classify_push_failure(combined_out)
                if is_push_err and reason:
                    delays = cap_delays if reason == "session_cap" else conflict_delays
                    retry_limit = len(delays) + 1
                    if attempt >= retry_limit - 1:
                        break
                    delay = delays[min(attempt, len(delays) - 1)]
                    label = "session cap" if reason == "session_cap" else "409 conflict"
                    logger.warning(
                        f"Kaggle push attempt {attempt + 1}/{retry_limit} hit {label}, backing off {delay}s..."
                    )
                    retry_line = (
                        f"[RETRY] attempt {attempt + 1} failed ({label}), "
                        f"backing off {delay}s...\n"
                    )
                    await asyncio.to_thread(
                        _append_text_file, log_file_path, retry_line
                    )
                    await asyncio.sleep(delay)
                    continue
                break

            output_tail = ""
            if out_str:
                output_tail += f"[OUTPUT]\n{out_str}\n"
            if err_str:
                output_tail += f"[STDERR]\n{err_str}\n"
            if output_tail:
                await asyncio.to_thread(_append_text_file, log_file_path, output_tail)

            is_error = proc.returncode != 0 or "Kernel push error" in out_str
            status = "error" if is_error else "queued"
            status_msg = err_str if is_error else out_str.strip()

            if is_error and (
                _classify_push_failure(out_str + "\n" + err_str) == "session_cap"
            ):
                from services.availability import (
                    format_held_sessions,
                    held_version_sessions,
                )

                held_note = format_held_sessions(
                    await held_version_sessions(account_username)
                )
                status = "session_cap"
                status_msg = (
                    f"Kaggle refused the push: '{account_username}' is at its "
                    f"batch GPU session limit "
                    f"({KAGGLE_MAX_GPU_SESSIONS_PER_ACCOUNT} concurrent). "
                    "This dashboard only tracks kernels it launched, so an account "
                    "that looks idle here can still be capped by a session running "
                    "on Kaggle. Cancel one at kaggle.com/code, then push again."
                    f"{held_note}"
                )

            url_match = re.search(
                r"https?://(?:www\.)?kaggle\.com/code/([a-zA-Z0-9_\-]+)/([a-zA-Z0-9_\-]+)",
                out_str,
            )
            if url_match:
                real_username = url_match.group(1)
                real_slug = url_match.group(2)
                kernel_ref = f"{real_username}/{real_slug}"
                kaggle_url = f"https://www.kaggle.com/code/{kernel_ref}"
                if real_username != account_username:
                    logger.info(
                        f"Auto-corrected account username from {account_username} to {real_username}"
                    )
                    AccountManager.update_username(account_username, real_username)
                    account_username = real_username

            run_record = {
                "id": run_id,
                "account_username": account_username,
                "kernel_slug": slug if not url_match else real_slug,
                "kernel_ref": kernel_ref,
                "title": clean_title,
                "code_file": str(code_path),
                "accelerator": accelerator,
                "enable_internet": 1 if enable_internet else 0,
                "is_trial": 1 if is_trial else 0,
                "timeout_seconds": effective_timeout,
                "status": status,
                "status_message": status_msg,
                "start_time": utcnow_iso(),
                "kaggle_url": kaggle_url,
                "workload_id": workload_id,
                "shard_index": shard_index,
                "total_shards": total_shards,
                "log_file": str(log_file_path),
            }
            create_run_record(run_record)

            if not is_error:
                max_streams = max(1, int(os.getenv("MAX_CONCURRENT_LOG_STREAMS", "6")))
                if len(cls._active_stream_processes) < max_streams:
                    asyncio.create_task(
                        cls.start_background_log_stream(
                            run_id, account_username, kernel_ref, log_file_path
                        )
                    )
                else:
                    logger.info(
                        f"Log streaming deferred for {run_id} ({len(cls._active_stream_processes)}/{max_streams} streams active) - will start on demand"
                    )

            return {
                "success": not is_error,
                "run_id": run_id,
                "kernel_ref": kernel_ref,
                "kaggle_url": kaggle_url,
                "status": status,
                "message": status_msg,
                "title": clean_title,
                "renamed": renamed,
            }
        except Exception as e:
            logger.error(f"Failed to push kernel {kernel_ref}: {e}")
            await asyncio.to_thread(
                _append_text_file, log_file_path, f"\n[EXCEPTION] {e!s}\n"
            )
            return {"success": False, "run_id": run_id, "error": str(e)}

    @staticmethod
    def _normalize_kernel_status(raw: str) -> str:
        return _normalize_kernel_status_fn(raw)

    @classmethod
    async def get_kernel_status(
        cls, account_username: str, kernel_ref: str
    ) -> dict[str, Any]:
        return await kaggle_status.get_kernel_status(account_username, kernel_ref)

    @classmethod
    async def fetch_full_logs(cls, account_username: str, kernel_ref: str) -> str:
        return await kaggle_status.fetch_full_logs(account_username, kernel_ref)

    @classmethod
    async def start_background_log_stream(
        cls, run_id: str, account_username: str, kernel_ref: str, log_file: Path
    ):
        await kaggle_logs.start_background_log_stream(
            run_id, account_username, kernel_ref, log_file
        )

    @classmethod
    def ensure_log_stream(cls, run: dict[str, Any]) -> None:
        kaggle_logs.ensure_log_stream(run)

    @classmethod
    def register_log_subscriber(cls, run_id: str) -> asyncio.Queue:
        return kaggle_logs.register_log_subscriber(run_id)

    @classmethod
    def unregister_log_subscriber(cls, run_id: str, queue: asyncio.Queue):
        kaggle_logs.unregister_log_subscriber(run_id, queue)

    @classmethod
    async def list_output_files(
        cls, account_username: str, kernel_ref: str
    ) -> list[dict[str, Any]]:
        return await kaggle_outputs.list_output_files(account_username, kernel_ref)

    @classmethod
    async def _run_versioned_helper(
        cls,
        account_username: str,
        args: list[str],
        timeout: int = VERSIONED_FETCH_TIMEOUT_SECONDS,
    ) -> str | None:
        return await kaggle_outputs.run_versioned_helper(
            account_username, args, timeout=timeout
        )

    @classmethod
    async def list_account_kernels(
        cls, account_username: str, search: str = "", page: int = 1, page_size: int = 20
    ) -> dict[str, Any]:
        return await kaggle_outputs.list_account_kernels(
            account_username, search, page, page_size
        )

    @classmethod
    async def list_kernel_versions(
        cls, account_username: str, kernel_ref: str, max_versions: int = 20
    ) -> dict[str, Any]:
        return await kaggle_outputs.list_kernel_versions(
            account_username, kernel_ref, max_versions
        )

    @classmethod
    async def fetch_version_log(
        cls, account_username: str, kernel_ref: str, version: int
    ) -> str:
        return await kaggle_outputs.fetch_version_log(
            account_username, kernel_ref, version
        )

    @classmethod
    async def get_kernel_current_version(
        cls, account_username: str, kernel_ref: str
    ) -> int | None:
        return await kaggle_outputs.get_kernel_current_version(
            account_username, kernel_ref
        )

    @classmethod
    async def download_outputs_of_version(
        cls, account_username: str, kernel_ref: str, version: int, run_id: str
    ) -> Path | None:
        return await kaggle_outputs.download_outputs_of_version(
            account_username, kernel_ref, version, run_id
        )

    @classmethod
    async def download_latest_outputs(
        cls,
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
            current = await cls.get_kernel_current_version(account_username, kernel_ref)
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
                got = await cls.download_outputs_of_version(
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
            plain = await cls.download_outputs(account_username, kernel_ref, run_id)
            return plain, None, diagnostics
        except Exception as e:
            diagnostics.append(f"plain pull failed: {e}")
            return None, None, diagnostics

    @classmethod
    async def download_external_outputs(
        cls, account_username: str, kernel_ref: str, version: int | None = None
    ):
        owner, _, slug = kernel_ref.partition("/")
        if not owner or not slug:
            return None, None, ["invalid kernel_ref"]
        safe_slug = cls.sanitize_slug(slug)
        safe_owner = (
            "".join(c for c in owner if c.isalnum() or c in ("-", "_")).lower()
            or "unknown"
        )
        run_id = f"ext_{safe_owner}_{safe_slug}"
        if version:
            got = await cls.download_outputs_of_version(
                account_username, kernel_ref, int(version), run_id
            )
            if got:
                return got, int(version), []
            try:
                cur = await cls.get_kernel_current_version(account_username, kernel_ref)
                if cur is None or int(version) == cur:
                    plain, _, diag = await cls.download_latest_outputs(
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
        return await cls.download_latest_outputs(account_username, kernel_ref, run_id)

    @classmethod
    async def _write_stop_stub(
        cls,
        stop_dir: Path,
        kernel_ref: str,
        title: str,
        is_notebook: bool,
        message: str,
    ) -> None:
        stop_dir.mkdir(parents=True, exist_ok=True)
        stub_filename = "cell.ipynb" if is_notebook else "main.py"
        if is_notebook:
            stub = {
                "cells": [
                    {
                        "cell_type": "code",
                        "execution_count": None,
                        "metadata": {},
                        "outputs": [],
                        "source": [f"print({message!r})\n"],
                    }
                ],
                "metadata": {"kernelspec": dict(cls.DEFAULT_KERNELSPEC)},
                "nbformat": 4,
                "nbformat_minor": 2,
            }
            await asyncio.to_thread(_write_json_file, stop_dir / stub_filename, stub)
        else:
            stop_code = f"print({message!r})\n"
            await asyncio.to_thread(
                _write_text_file, stop_dir / stub_filename, stop_code
            )
        metadata = {
            "id": kernel_ref,
            "title": (title or "")[:50],
            "code_file": stub_filename,
            "language": "python",
            "kernel_type": "notebook" if is_notebook else "script",
            "is_private": "true",
            "enable_gpu": "false",
            "enable_tpu": "false",
            "enable_internet": "false",
        }
        await asyncio.to_thread(
            _write_json_file, stop_dir / "kernel-metadata.json", metadata
        )

    @classmethod
    async def stop_external_kernel(
        cls, account_username: str, kernel_ref: str, title: str | None = None
    ) -> dict[str, Any]:
        owner, _, slug = kernel_ref.partition("/")
        if not owner or not slug:
            return {"success": False, "error": "Invalid kernel_ref"}
        run_id = f"ext_stop_{owner}_{slug}_{uuid.uuid4().hex[:4]}"
        stop_dir = NOTEBOOKS_DIR / f"stop_{run_id}"
        await cls._write_stop_stub(
            stop_dir,
            kernel_ref,
            title or slug,
            True,
            "Session explicitly stopped from Account Kernels Explorer.",
        )
        cli = get_kaggle_cli_path()
        cmd = [cli, "kernels", "push", "-p", str(stop_dir), "-t", "60"]
        env = AccountManager.get_account_env(account_username)
        tracker.begin(f"ext_stop:{account_username}/{kernel_ref}")
        proc = None
        try:
            async with cls._get_push_semaphore():
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            out_str = strip_kaggle_cli_warnings(stdout.decode("utf-8", errors="ignore"))
            err_str = strip_kaggle_cli_warnings(stderr.decode("utf-8", errors="ignore"))
            combined = (out_str + "\n" + err_str).strip()
            if proc.returncode == 0 and "kernel push error" not in combined.lower():
                return {"success": True, "message": f"Stop signal sent to {kernel_ref}"}
            return {"success": False, "error": combined[-500:] or "push failed"}
        except TimeoutError:
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except Exception:
                    pass
            return {"success": False, "error": "stop push timed out"}
        except Exception as e:
            return {"success": False, "error": str(e)}
        finally:
            tracker.end(f"ext_stop:{account_username}/{kernel_ref}")

    @classmethod
    async def download_outputs(
        cls, account_username: str, kernel_ref: str, run_id: str
    ) -> Path:
        return await kaggle_outputs.download_outputs(
            account_username, kernel_ref, run_id
        )

    @classmethod
    async def stop_kernel(cls, run_id: str) -> dict[str, Any]:
        tracker.begin(run_stop_key(run_id))
        try:
            async with cls._get_stop_semaphore():
                return await cls._stop_kernel_impl(run_id)
        finally:
            tracker.end(run_stop_key(run_id))

    @classmethod
    async def _stop_kernel_impl(cls, run_id: str) -> dict[str, Any]:
        run = get_run_by_id(run_id)
        if not run:
            return {"success": False, "error": "Run not found"}

        account_username = run["account_username"]
        kernel_ref = run["kernel_ref"]
        title = run["title"]

        kaggle_logs.stop_log_stream(run_id)

        try:
            saved_dir = await cls.download_outputs(account_username, kernel_ref, run_id)
            n_saved = sum(1 for p in saved_dir.rglob("*") if p.is_file())
            logger.info(
                f"Pre-stop output snapshot for {run_id}: {n_saved} file(s) preserved."
            )
        except Exception as e:
            logger.warning(
                f"Pre-stop output snapshot failed for {run_id} (continuing): {e}"
            )

        cancelled_version = await cls.get_kernel_current_version(
            account_username, kernel_ref
        )

        stop_dir = NOTEBOOKS_DIR / f"stop_{run_id}"
        is_notebook = (run["code_file"] or "").endswith(".ipynb")
        await cls._write_stop_stub(
            stop_dir,
            kernel_ref,
            title,
            is_notebook,
            "Session explicitly stopped by Kaggle Automation Dashboard.",
        )

        cli = get_kaggle_cli_path()
        cmd = [cli, "kernels", "push", "-p", str(stop_dir), "-t", "60"]
        env = AccountManager.get_account_env(account_username)

        push_ok = False
        last_err = ""
        stop_delays = cls.CONFLICT_RETRY_DELAYS
        cap_delays = cls.SESSION_CAP_RETRY_DELAYS
        for attempt in range(max(len(stop_delays), len(cap_delays)) + 1):
            proc = None
            try:
                async with cls._get_push_semaphore():
                    proc = await asyncio.create_subprocess_exec(
                        *cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        env=env,
                    )
                    try:
                        stdout, stderr = await asyncio.wait_for(
                            proc.communicate(), timeout=120
                        )
                    except TimeoutError:
                        if proc.returncode is None:
                            try:
                                proc.kill()
                            except Exception:
                                pass
                            try:
                                await asyncio.wait_for(proc.wait(), timeout=5)
                            except Exception:
                                pass
                        raise
                out_str = strip_kaggle_cli_warnings(
                    stdout.decode("utf-8", errors="ignore")
                )
                err_str = strip_kaggle_cli_warnings(
                    stderr.decode("utf-8", errors="ignore")
                )
                combined = (out_str + "\n" + err_str).strip()
                last_err = combined[-500:]
                is_push_err = "kernel push error" in combined.lower()
                if proc.returncode == 0 and not is_push_err:
                    push_ok = True
                    break
                reason = _classify_push_failure(combined)
                delays = cap_delays if reason == "session_cap" else stop_delays
                if reason and attempt < len(delays):
                    delay = delays[attempt]
                    label = "session cap" if reason == "session_cap" else "409 Conflict"
                    logger.warning(
                        f"Stop push hit {label}, retry {attempt + 1}/{len(delays)} in {delay}s..."
                    )
                    await asyncio.sleep(delay)
                    continue
                break
            except Exception as e:
                logger.warning(f"Stop push completed with warning: {e}")
                last_err = str(e)
                break

        if push_ok:
            update_run_status(
                run_id, "stopped", "Explicitly stopped by user", utcnow_iso()
            )
            for r in get_active_runs():
                if r["kernel_ref"] == kernel_ref and r["id"] != run_id:
                    update_run_status(
                        r["id"],
                        "stopped",
                        "Stopped: shared kernel received the stop push",
                        utcnow_iso(),
                    )

            if cancelled_version:
                set_run_output_version(run_id, cancelled_version)
                for r in get_active_runs():
                    if r["kernel_ref"] == kernel_ref and r["id"] != run_id:
                        set_run_output_version(r["id"], cancelled_version)

                got = None
                for delay in cls.STOP_CAPTURE_RETRY_DELAYS:
                    await asyncio.sleep(delay)
                    got = await cls.download_outputs_of_version(
                        account_username, kernel_ref, cancelled_version, run_id
                    )
                    if got:
                        logger.info(
                            f"Recovered cancelled version {cancelled_version} output "
                            f"of {kernel_ref} into data/outputs/{run_id}"
                        )
                        break
                if not got:
                    logger.info(
                        f"Cancelled version {cancelled_version} of {kernel_ref} had no "
                        f"recoverable output (run may have published nothing yet)."
                    )
                    extra = (
                        f" Cancelled version {cancelled_version} published no output "
                        f"files yet - only its log."
                    )
                else:
                    extra = f" Recovered {len(list(got.glob('*')))} file(s) from cancelled version {cancelled_version}."
            else:
                extra = " (Could not determine the cancelled version number - pull may return the stub log.)"

            return {
                "success": True,
                "message": f"Run {run_id} ({kernel_ref}) stopped successfully.{extra}",
            }

        update_run_status(run_id, run["status"], f"Stop failed: {last_err}")
        return {
            "success": False,
            "error": f"Failed to stop run {run_id}: {last_err or 'unknown CLI error'}",
        }
