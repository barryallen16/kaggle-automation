import asyncio
import csv
import hashlib
import io
import json
import logging
import os
import shutil
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from config import (
    ACCOUNTS_DIR,
    KAGGLE_APIKEYS_RAW,
    get_kaggle_cli_path,
    is_gpu_accelerator,
)
from database import (
    delete_account as db_delete_account,
)
from database import (
    get_account_by_username,
    get_active_runs,
    get_all_accounts,
    save_account,
)

from services.kaggle_kernel_identity import strip_kaggle_cli_warnings

logger = logging.getLogger("account_manager")


def _write_text_file(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


QUOTA_CAP_ENABLED = os.getenv("AUTO_QUOTA_RUNTIME_CAP", "1") == "1"
SESSION_CAP_MINUTES = 12 * 60
PRE_LOOP_ALLOWANCE_MINUTES = 60
FINISH_SLOP_MINUTES = 10
QUOTA_CAP_MIN_INJECT_MINUTES = 10
QUOTA_CAP_MAX_QUOTA_AGE_MINUTES = 15


class AccountManager:
    _lock: ClassVar[asyncio.Lock | None] = None
    _lock_loop_id: ClassVar[int | None] = None

    _username_aliases: ClassVar[dict[str, str]] = {}

    QUOTA_CONCURRENCY_LIMIT = max(1, int(os.getenv("QUOTA_REFRESH_CONCURRENCY", "3")))
    QUOTA_CALL_TIMEOUT_SECONDS = int(os.getenv("QUOTA_CALL_TIMEOUT_SECONDS", "90"))
    _quota_semaphore: ClassVar[asyncio.Semaphore | None] = None
    _quota_semaphore_loop_id: ClassVar[int | None] = None

    _refresh_all_task: ClassVar[asyncio.Task | None] = None
    _refresh_all_task_loop_id: ClassVar[int | None] = None

    @classmethod
    def _get_lock(cls) -> asyncio.Lock:
        loop_id = id(asyncio.get_running_loop())
        if cls._lock is None or cls._lock_loop_id != loop_id:
            cls._lock = asyncio.Lock()
            cls._lock_loop_id = loop_id
        return cls._lock

    @classmethod
    def _get_quota_semaphore(cls) -> asyncio.Semaphore:
        loop_id = id(asyncio.get_running_loop())
        if cls._quota_semaphore is None or cls._quota_semaphore_loop_id != loop_id:
            cls._quota_semaphore = asyncio.Semaphore(cls.QUOTA_CONCURRENCY_LIMIT)
            cls._quota_semaphore_loop_id = loop_id
        return cls._quota_semaphore

    @classmethod
    def resolve_effective_username(cls, username: str) -> str:
        current = username or ""
        seen = 0
        while current in cls._username_aliases and seen < 32:
            current = cls._username_aliases[current]
            seen += 1
        return current

    @staticmethod
    def get_account_config_dir(username_or_id: str) -> Path:
        safe_id = "".join(
            c for c in username_or_id if c.isalnum() or c in ("-", "_")
        ).lower()
        acc_dir = ACCOUNTS_DIR / safe_id / ".kaggle"
        acc_dir.mkdir(parents=True, exist_ok=True)
        return acc_dir

    @classmethod
    def setup_account_files(cls, username: str, api_key_or_token: str) -> Path:
        acc_dir = cls.get_account_config_dir(username)

        stripped = api_key_or_token.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                parsed = json.loads(stripped)
                kaggle_json_path = acc_dir / "kaggle.json"
                with open(kaggle_json_path, "w", encoding="utf-8") as f:
                    json.dump(parsed, f, indent=2)
            except Exception:
                pass

        token_path = acc_dir / "access_token"
        with open(token_path, "w", encoding="utf-8") as f:
            f.write(stripped)

        return acc_dir

    @staticmethod
    def _read_access_token(acc_dir: Path) -> str | None:
        try:
            token = (acc_dir / "access_token").read_text(encoding="utf-8").strip()
            return token or None
        except OSError:
            return None

    @classmethod
    def get_account_env(cls, username: str) -> dict[str, str]:
        username = cls.resolve_effective_username(username)
        acc_dir = cls.get_account_config_dir(username)
        env = os.environ.copy()
        env["KAGGLE_CONFIG_DIR"] = str(acc_dir)

        token = cls._read_access_token(acc_dir)
        if token and not token.startswith("{"):
            env["KAGGLE_API_TOKEN"] = token
        else:
            env.pop("KAGGLE_API_TOKEN", None)

        bin_folder = "Scripts" if os.name == "nt" else "bin"
        venv_bin = str(Path(sys.prefix) / bin_folder)
        if venv_bin not in env.get("PATH", ""):
            env["PATH"] = venv_bin + os.pathsep + env.get("PATH", "")

        env["PYTHONIOENCODING"] = "utf-8"

        return env

    @classmethod
    def extract_username_from_token(cls, key: str) -> str | None:
        stripped = key.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                data = json.loads(stripped)
                if "username" in data:
                    return data["username"]
            except Exception:
                pass

        parts = stripped.split(".")
        if len(parts) == 3:
            try:
                import base64

                payload = parts[1]
                payload += "=" * ((4 - len(payload) % 4) % 4)
                decoded = base64.urlsafe_b64decode(payload)
                claims = json.loads(decoded)
                for field in ["username", "user_name", "sub", "preferred_username"]:
                    if (
                        field in claims
                        and isinstance(claims[field], str)
                        and not claims[field].isdigit()
                    ):
                        return claims[field]
            except Exception:
                pass
        return None

    @classmethod
    async def fetch_username_for_key(cls, temp_id: str, key: str) -> str:
        extracted = cls.extract_username_from_token(key)
        if extracted:
            return extracted

        acc_dir = cls.get_account_config_dir(temp_id)
        token_path = acc_dir / "access_token"
        await asyncio.to_thread(_write_text_file, token_path, key.strip())

        env = cls.get_account_env(temp_id)
        cli = get_kaggle_cli_path()

        for subcmd in [
            ["kernels", "list", "-m", "-v"],
            ["datasets", "list", "-m", "-v"],
            ["models", "list", "-m", "-v"],
        ]:
            proc = None
            try:
                async with cls._get_quota_semaphore():
                    proc = await asyncio.create_subprocess_exec(
                        cli,
                        *subcmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        env=env,
                    )
                    stdout, _stderr = await asyncio.wait_for(
                        proc.communicate(), timeout=30
                    )
                out_str = strip_kaggle_cli_warnings(
                    stdout.decode("utf-8", errors="ignore")
                )

                if out_str:
                    csv_reader = csv.DictReader(io.StringIO(out_str))
                    rows = list(csv_reader)
                    if rows and "ref" in rows[0]:
                        return rows[0]["ref"].split("/")[0]
            except TimeoutError:
                logger.warning(f"Timed out querying {subcmd}")
            except Exception as e:
                logger.error(f"Error querying {subcmd}: {e}")
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

        return f"kaggle_{hashlib.sha256(key.encode('utf-8')).hexdigest()[:8]}"

    @classmethod
    def _cleanup_temp_dir(cls, temp_id: str):
        temp_dir = cls.get_account_config_dir(temp_id).parent
        if temp_dir.exists():
            try:
                shutil.rmtree(temp_dir)
            except Exception:
                pass

    @classmethod
    def update_username(cls, old_username: str, new_username: str):
        from database import update_account_username

        if old_username == new_username:
            return

        old_dir = cls.get_account_config_dir(old_username)
        new_dir = cls.get_account_config_dir(new_username)
        try:
            if old_dir != new_dir:
                shutil.copytree(old_dir, new_dir, dirs_exist_ok=True)
        except Exception as e:
            logger.error(f"Failed to mirror config dir {old_dir} -> {new_dir}: {e}")

        cls._username_aliases[old_username] = new_username
        for key, val in list(cls._username_aliases.items()):
            if val == old_username:
                cls._username_aliases[key] = new_username

        update_account_username(old_username, new_username)

    @classmethod
    async def fetch_quota(cls, username: str) -> dict[str, Any]:
        try:
            async with cls._get_quota_semaphore():
                return await asyncio.wait_for(
                    cls._fetch_quota_unthrottled(username),
                    timeout=cls.QUOTA_CALL_TIMEOUT_SECONDS,
                )
        except TimeoutError:
            logger.warning(
                f"kaggle quota timed out after {cls.QUOTA_CALL_TIMEOUT_SECONDS}s for {username}"
            )
            return {
                "raw": [],
                "gpu": {
                    "name": "GPU (T4 x 2)",
                    "used": 0,
                    "limit": 30,
                    "unit": "hours",
                    "percent": 0,
                },
                "tpu": {
                    "name": "TPU VM v3-8",
                    "used": 0,
                    "limit": 20,
                    "unit": "hours",
                    "percent": 0,
                },
                "error": f"quota lookup timed out after {cls.QUOTA_CALL_TIMEOUT_SECONDS}s",
            }

    @staticmethod
    def _parse_seconds_str(val: Any) -> float | None:
        if val is None:
            return None
        s = str(val).strip().lower()
        if s.endswith("s"):
            s = s[:-1].strip()
        try:
            return float(s)
        except ValueError:
            return None

    @classmethod
    async def _fetch_quota_http(cls, token: str) -> dict[str, Any] | None:
        import httpx

        base = (os.getenv("KAGGLE_API_BASE") or "https://api.kaggle.com").rstrip("/")
        url = f"{base}/v1/kernels.KernelsApiService/GetAcceleratorQuotaStatistics"
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(
                    url,
                    json={},
                    headers={"Authorization": f"Bearer {token}"},
                )
                if resp.status_code >= 400:
                    return None
                data = resp.json()
        except Exception:
            return None
        if not isinstance(data, dict) or (
            "gpuQuota" not in data and "tpuQuota" not in data
        ):
            return None

        def _entry(q: dict | None, default_name: str, default_limit: float) -> dict[str, Any]:
            q = q or {}
            used_s = cls._parse_seconds_str(q.get("timeUsed")) or 0.0
            limit_s = cls._parse_seconds_str(q.get("totalTimeAllowed"))
            reserved_s = cls._parse_seconds_str(q.get("timeReserved"))
            used_h = round(used_s / 3600.0, 2)
            limit_h = round(limit_s / 3600.0, 2) if limit_s and limit_s > 0 else default_limit
            pct = min(100.0, round((used_h / limit_h) * 100, 1)) if limit_h > 0 else 0.0
            out: dict[str, Any] = {
                "name": default_name,
                "used": used_h,
                "limit": limit_h,
                "unit": "hours",
                "percent": pct,
            }
            if reserved_s is not None:
                out["reserved_seconds"] = reserved_s
            return out

        return {
            "raw": [data],
            "gpu": _entry(data.get("gpuQuota"), "GPU (T4 x 2)", 30.0),
            "tpu": _entry(data.get("tpuQuota"), "TPU VM v3-8", 20.0),
        }

    @classmethod
    async def _fetch_quota_unthrottled(cls, username: str) -> dict[str, Any]:
        env = cls.get_account_env(username)
        token = (env.get("KAGGLE_API_TOKEN") or "").strip()
        if token:
            http_summary = await cls._fetch_quota_http(token)
            if http_summary is not None:
                return http_summary

        cli = get_kaggle_cli_path()
        cmd = [cli, "quota", "-v"]
        proc = None

        try:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
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
            out_str = strip_kaggle_cli_warnings(
                stdout.decode("utf-8", errors="ignore")
            )
            err_str = strip_kaggle_cli_warnings(
                stderr.decode("utf-8", errors="ignore")
            )

            if proc.returncode != 0:
                logger.warning(
                    f"kaggle quota failed for {username} (rc={proc.returncode}): {err_str.strip()}"
                )

            quotas = []
            if out_str:
                try:
                    csv_reader = csv.DictReader(io.StringIO(out_str))
                    quotas.extend(csv_reader)
                except Exception as e:
                    logger.error(f"Failed to parse quota CSV for {username}: {e}")

            summary = {
                "raw": quotas,
                "gpu": {
                    "name": "GPU (T4 x 2)",
                    "used": 0,
                    "limit": 30,
                    "unit": "hours",
                    "percent": 0,
                },
                "tpu": {
                    "name": "TPU VM v3-8",
                    "used": 0,
                    "limit": 20,
                    "unit": "hours",
                    "percent": 0,
                },
            }

            def _hours(value_str) -> float | None:
                if value_str is None:
                    return None
                cleaned = (
                    str(value_str)
                    .lower()
                    .replace("hours", "")
                    .replace("hour", "")
                    .replace("h", "")
                    .strip()
                )
                try:
                    return float(cleaned)
                except ValueError:
                    return None

            for q in quotas:
                acc_name = (q.get("resource") or q.get("accelerator") or "").lower()
                raw_used = q.get("used")
                limit_val = (
                    _hours(q.get("total")) if q.get("total") else _hours(q.get("limit"))
                )
                used_val = _hours(raw_used)
                if used_val is None or limit_val is None or limit_val <= 0:
                    continue
                pct = round((used_val / limit_val) * 100, 1)
                entry = {
                    "name": q.get("resource") or q.get("accelerator") or "GPU",
                    "used": used_val,
                    "limit": limit_val,
                    "unit": "hours",
                    "percent": min(100.0, pct),
                }
                if "tpu" in acc_name:
                    summary["tpu"] = entry
                elif "gpu" in acc_name:
                    summary["gpu"] = entry

            return summary
        except Exception as e:
            logger.error(f"Error fetching quota for {username}: {e}")
            return {
                "raw": [],
                "gpu": {
                    "name": "GPU (T4 x 2)",
                    "used": 0,
                    "limit": 30,
                    "unit": "hours",
                    "percent": 0,
                },
                "tpu": {
                    "name": "TPU VM v3-8",
                    "used": 0,
                    "limit": 20,
                    "unit": "hours",
                    "percent": 0,
                },
                "error": str(e),
            }

    @classmethod
    async def add_account(
        cls, api_key_or_token: str, custom_username: str | None = None
    ) -> dict[str, Any]:
        async with cls._get_lock():
            stripped_key = api_key_or_token.strip()

            if not custom_username:
                existing = next(
                    (
                        a
                        for a in get_all_accounts()
                        if a.get("api_key", "").strip() == stripped_key
                    ),
                    None,
                )
                if existing:
                    username = existing["username"]
                    cls.setup_account_files(username, stripped_key)
                    return {
                        "id": existing["id"],
                        "username": username,
                        "quota": existing.get("last_quota"),
                        "existing": True,
                    }

            temp_id = uuid.uuid4().hex[:8]
            if custom_username:
                username = custom_username.strip()
            else:
                username = await cls.fetch_username_for_key(temp_id, api_key_or_token)
                cls._cleanup_temp_dir(temp_id)

            cls.setup_account_files(username, api_key_or_token)

            quota_data = await cls.fetch_quota(username)

            save_account(temp_id, username, api_key_or_token, quota_data)
            saved = get_account_by_username(username)
            actual_id = saved["id"] if saved else temp_id

            return {"id": actual_id, "username": username, "quota": quota_data}

    @classmethod
    async def initialize_from_env(cls):
        if not KAGGLE_APIKEYS_RAW:
            return

        keys = [k.strip() for k in KAGGLE_APIKEYS_RAW.split(",") if k.strip()]
        for key in keys:
            try:
                await cls.add_account(key)
            except Exception as e:
                logger.error(f"Failed to auto-init account from .env: {e}")

    @classmethod
    async def _run_refresh_all(cls) -> list[dict[str, Any]]:
        try:
            accounts = get_all_accounts()
            results = await asyncio.gather(
                *(cls.refresh_account_quota(acc["username"]) for acc in accounts),
                return_exceptions=True,
            )
            for acc, res in zip(accounts, results):
                if isinstance(res, Exception):
                    logger.warning(
                        f"Quota refresh failed for @{acc['username']}: {res}"
                    )
            return get_all_accounts()
        finally:
            if cls._refresh_all_task is asyncio.current_task():
                cls._refresh_all_task = None
                cls._refresh_all_task_loop_id = None

    @classmethod
    async def refresh_all_quotas(cls) -> list[dict[str, Any]]:
        loop_id = id(asyncio.get_running_loop())
        existing = cls._refresh_all_task
        if (
            existing is not None
            and cls._refresh_all_task_loop_id == loop_id
            and not existing.done()
        ):
            logger.info(
                "Quota refresh already in progress - waiting for it instead of starting another."
            )
            return await asyncio.shield(existing)

        task = asyncio.create_task(cls._run_refresh_all())
        cls._refresh_all_task = task
        cls._refresh_all_task_loop_id = loop_id
        return await asyncio.shield(task)

    @classmethod
    async def refresh_account_quota(cls, username: str) -> dict[str, Any]:
        acc = get_account_by_username(username)
        if not acc:
            return {}
        cls.setup_account_files(username, acc["api_key"])
        quota = await cls.fetch_quota(username)
        save_account(acc["id"], username, acc["api_key"], quota)
        try:
            from services.session_monitor import SessionMonitor

            SessionMonitor._quota_cache.pop(username, None)
        except Exception:
            pass
        return quota

    @staticmethod
    def compute_gpu_runtime_budget_minutes(
        used_hours, limit_hours, concurrent_sessions: int
    ) -> int | None:
        try:
            used = float(used_hours)
            limit = float(limit_hours)
        except (TypeError, ValueError):
            return None
        if used < 0 or limit <= 0:
            return None
        remaining_h = limit - used
        if remaining_h <= 0:
            return None
        burners = max(1, int(concurrent_sessions or 1))
        runway_min = remaining_h * 60.0 / burners
        if runway_min >= SESSION_CAP_MINUTES:
            return None
        budget_min = runway_min - PRE_LOOP_ALLOWANCE_MINUTES - FINISH_SLOP_MINUTES
        if budget_min < QUOTA_CAP_MIN_INJECT_MINUTES:
            return None
        return int(budget_min)

    @classmethod
    async def gpu_runtime_budget_minutes(
        cls, username: str, concurrent_sessions: int
    ) -> int | None:
        if not QUOTA_CAP_ENABLED:
            return None
        acc = get_account_by_username(username)
        if not acc:
            return None
        quota = acc.get("last_quota") or {}
        try:
            checked = acc.get("last_checked")
            if checked:
                dt = datetime.fromisoformat(str(checked))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                age_min = (datetime.now(UTC) - dt).total_seconds() / 60.0
            else:
                age_min = None
            if age_min is None or age_min > QUOTA_CAP_MAX_QUOTA_AGE_MINUTES:
                refreshed = await cls.refresh_account_quota(username)
                if refreshed:
                    quota = refreshed
        except Exception as e:
            logger.warning(
                f"Live quota refresh failed for @{username} at launch - "
                f"using stored quota: {e}"
            )
        gpu = quota.get("gpu") or {}
        if gpu.get("error") or quota.get("error"):
            return None
        return cls.compute_gpu_runtime_budget_minutes(
            gpu.get("used"), gpu.get("limit"), concurrent_sessions
        )

    @staticmethod
    def active_gpu_runs(username: str) -> list[dict]:
        return [
            r
            for r in get_active_runs()
            if r.get("account_username") == username
            and is_gpu_accelerator(r.get("accelerator"))
        ]

    @classmethod
    def remove_account(cls, username: str):
        db_delete_account(username)
