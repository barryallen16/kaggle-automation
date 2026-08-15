import os
import io
import csv
import json
import uuid
import hashlib
import sys
import asyncio
import shutil
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional
from app.config import ACCOUNTS_DIR, KAGGLE_APIKEYS_RAW, get_kaggle_cli_path
from app.database import (
    save_account, get_all_accounts, get_account_by_username,
    delete_account as db_delete_account
)

logger = logging.getLogger("account_manager")

# ---------------------------------------------------------------------------
# Quota-aware runtime caps (self-finishing kernels)
# ---------------------------------------------------------------------------
# When an account's remaining weekly GPU quota cannot cover what a session can
# consume, the dashboard injects MAX_RUNTIME_MINUTES into the pushed kernel so
# the batch script stops itself cleanly BEFORE the quota runs out. Kaggle does
# not kill a session whose quota is spent - it hangs it "running" with no
# progress, which is exactly the case session_monitor force-stops with a stub
# (and stub cancels lose the version's output snapshot). A self-finishing
# kernel finalizes as "complete" instead and publishes its partial
# /kaggle/working normally.
#
# Mental model (all in MINUTES): a kernel can consume up to its full session
# before Kaggle's 12h hard cap force-stops it (that auto-stop DOES publish
# output). The script self-finishes at 11h measured from its inference loop,
# i.e. up to ~12h of session time once install+model load are counted. So the
# quota is the binding constraint whenever the account's remaining quota
# divided across concurrent burners is UNDER 12h - cap exactly then, and only
# then. If remaining per session is >= 12h the session cap ends the run first
# and the quota can never bind.
# Set AUTO_QUOTA_RUNTIME_CAP=0 to disable injection entirely.
QUOTA_CAP_ENABLED = os.getenv("AUTO_QUOTA_RUNTIME_CAP", "1") == "1"
# Kaggle's hard per-session cap (force-stop at 12h, output still publishes).
SESSION_CAP_MINUTES = 12 * 60
# Session start -> inference loop: queue + installs + model download/load.
# Measured ~11 min on the fitcheck runs (relabel log +633s, task_b log +686s)
# with queue time on top - and the batch scripts themselves reserve 60 min of
# the 12h session for it, so the cap uses the same 60-min allowance to stay
# consistent with how the scripts' 11h self-finish default is anchored.
PRE_LOOP_ALLOWANCE_MINUTES = 60
# One final item's latency + quota-accounting staleness (DB quota ~5 min old).
FINISH_SLOP_MINUTES = 10
# Below this a session can't do meaningful work after the ~11 min load.
QUOTA_CAP_MIN_INJECT_MINUTES = 10
# Max age (minutes) of the STORED last_quota before it is refreshed live at
# launch. The cap is only as good as the "remaining" number it is computed
# from: a stale row (dashboard restarted, no monitor running between waves)
# is exactly how a capped script can still outrun the quota that actually
# applies. While runs are active the monitor refreshes every ~5 min, so rows
# stay fresh and normal launches never pay for a CLI call.
QUOTA_CAP_MAX_QUOTA_AGE_MINUTES = 15

class AccountManager:
    _lock = asyncio.Lock()

    # Quota lookups each spawn a full kaggle CLI process (~50-150MB). Firing
    # one per account SIMULTANEOUSLY (15+ accounts) spikes RAM until the OS
    # OOM-killer takes down the whole server, and trips Kaggle rate limits.
    # All quota subprocesses therefore funnel through this global cap.
    QUOTA_CONCURRENCY_LIMIT = max(1, int(os.getenv("QUOTA_REFRESH_CONCURRENCY", "3")))
    QUOTA_CALL_TIMEOUT_SECONDS = int(os.getenv("QUOTA_CALL_TIMEOUT_SECONDS", "90"))
    _quota_semaphore: Optional[asyncio.Semaphore] = None

    # Single-flight guard: overlapping refresh-all triggers (double-clicks,
    # monitor quota probes, dashboard polling) share ONE run instead of
    # stacking another N subprocess batches on top of the first.
    _refresh_all_lock: Optional[asyncio.Lock] = None
    _sync_primitive_loop_id: Optional[int] = None

    @classmethod
    def _get_quota_semaphore(cls) -> asyncio.Semaphore:
        # Rebuilt if the running event loop changed (tests / embedded runners);
        # asyncio primitives bind to whichever loop first awaits them.
        loop_id = id(asyncio.get_running_loop())
        if cls._quota_semaphore is None or cls._sync_primitive_loop_id != loop_id:
            cls._quota_semaphore = asyncio.Semaphore(cls.QUOTA_CONCURRENCY_LIMIT)
            cls._sync_primitive_loop_id = loop_id
        return cls._quota_semaphore

    @classmethod
    def _get_refresh_all_lock(cls) -> asyncio.Lock:
        loop_id = id(asyncio.get_running_loop())
        if cls._refresh_all_lock is None or cls._sync_primitive_loop_id != loop_id:
            cls._refresh_all_lock = asyncio.Lock()
            cls._sync_primitive_loop_id = loop_id
        return cls._refresh_all_lock

    @staticmethod
    def get_account_config_dir(username_or_id: str) -> Path:
        """Returns the isolated config directory for a specific Kaggle account."""
        safe_id = "".join(c for c in username_or_id if c.isalnum() or c in ("-", "_")).lower()
        acc_dir = ACCOUNTS_DIR / safe_id / ".kaggle"
        acc_dir.mkdir(parents=True, exist_ok=True)
        return acc_dir

    @classmethod
    def setup_account_files(cls, username: str, api_key_or_token: str) -> Path:
        """Writes the access_token and/or kaggle.json to the account's isolated config directory."""
        acc_dir = cls.get_account_config_dir(username)
        
        # Check if the key is JSON (kaggle.json format) or raw access token
        stripped = api_key_or_token.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                parsed = json.loads(stripped)
                kaggle_json_path = acc_dir / "kaggle.json"
                with open(kaggle_json_path, "w", encoding="utf-8") as f:
                    json.dump(parsed, f, indent=2)
            except Exception:
                pass

        # Write access_token file (used by modern kaggle CLI auth)
        token_path = acc_dir / "access_token"
        with open(token_path, "w", encoding="utf-8") as f:
            f.write(stripped)

        return acc_dir

    @staticmethod
    def _read_access_token(acc_dir: Path) -> Optional[str]:
        """Reads the access token written for this account's isolated config dir."""
        try:
            token = (acc_dir / "access_token").read_text(encoding="utf-8").strip()
            return token or None
        except OSError:
            return None

    @classmethod
    def get_account_env(cls, username: str) -> Dict[str, str]:
        """Builds subprocess environment variables for the specified account."""
        acc_dir = cls.get_account_config_dir(username)
        env = os.environ.copy()
        env["KAGGLE_CONFIG_DIR"] = str(acc_dir)

        # The kaggle CLI (>= 2.x / kagglesdk) authenticates access tokens ONLY from
        # the KAGGLE_API_TOKEN env var or ~/.kaggle/access_token - it never reads
        # $KAGGLE_CONFIG_DIR/access_token. Export the account's token here so each
        # subprocess authenticates as the right account without touching ~/.kaggle.
        token = cls._read_access_token(acc_dir)
        if token and not token.startswith("{"):
            env["KAGGLE_API_TOKEN"] = token
        else:
            env.pop("KAGGLE_API_TOKEN", None)

        # Ensure virtualenv Scripts/bin is at the front of PATH
        bin_folder = "Scripts" if os.name == "nt" else "bin"
        venv_bin = str(Path(sys.prefix) / bin_folder)
        if venv_bin not in env.get("PATH", ""):
            env["PATH"] = venv_bin + os.pathsep + env.get("PATH", "")

        # Force UTF-8 output encoding for kaggle CLI (fixes Windows cp1252 errors)
        env["PYTHONIOENCODING"] = "utf-8"

        return env

    @classmethod
    def extract_username_from_token(cls, key: str) -> Optional[str]:
        """Tries to extract username from kaggle.json or JWT token claims."""
        stripped = key.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                data = json.loads(stripped)
                if "username" in data:
                    return data["username"]
            except Exception:
                pass
        
        # Check if JWT format (header.payload.signature)
        parts = stripped.split(".")
        if len(parts) == 3:
            try:
                import base64
                payload = parts[1]
                # Add padding if needed
                payload += "=" * ((4 - len(payload) % 4) % 4)
                decoded = base64.urlsafe_b64decode(payload)
                claims = json.loads(decoded)
                for field in ["username", "user_name", "sub", "preferred_username"]:
                    if field in claims and isinstance(claims[field], str) and not claims[field].isdigit():
                        return claims[field]
            except Exception:
                pass
        return None

    @classmethod
    async def fetch_username_for_key(cls, temp_id: str, key: str) -> str:
        """Resolves Kaggle username by querying JWT claims or Kaggle CLI."""
        extracted = cls.extract_username_from_token(key)
        if extracted:
            return extracted

        acc_dir = cls.get_account_config_dir(temp_id)
        token_path = acc_dir / "access_token"
        with open(token_path, "w", encoding="utf-8") as f:
            f.write(key.strip())

        env = cls.get_account_env(temp_id)
        cli = get_kaggle_cli_path()
        
        # Try kernels list, datasets list, models list
        for subcmd in [["kernels", "list", "-m", "-v"], ["datasets", "list", "-m", "-v"], ["models", "list", "-m", "-v"]]:
            try:
                proc = await asyncio.create_subprocess_exec(
                    cli,
                    *subcmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env
                )
                stdout, stderr = await proc.communicate()
                out_str = stdout.decode("utf-8", errors="ignore")
                
                if out_str:
                    csv_reader = csv.DictReader(io.StringIO(out_str))
                    rows = list(csv_reader)
                    if rows and "ref" in rows[0]:
                        return rows[0]["ref"].split("/")[0]
            except Exception as e:
                logger.error(f"Error querying {subcmd}: {e}")

        return f"kaggle_{hashlib.sha256(key.encode('utf-8')).hexdigest()[:8]}"

    @classmethod
    def _cleanup_temp_dir(cls, temp_id: str):
        """Removes the temporary config directory used during username resolution."""
        temp_dir = cls.get_account_config_dir(temp_id).parent
        if temp_dir.exists():
            try:
                shutil.rmtree(temp_dir)
            except Exception:
                pass

    @classmethod
    def update_username(cls, old_username: str, new_username: str):
        """Renames an account's folder and updates database records."""
        from app.database import update_account_username
        if old_username == new_username:
            return

        # Update config directory
        old_dir = cls.get_account_config_dir(old_username)
        new_dir = cls.get_account_config_dir(new_username)
        if old_dir.exists():
            try:
                # get_account_config_dir() pre-creates the destination, so we must merge into it
                shutil.copytree(old_dir, new_dir, dirs_exist_ok=True)
                shutil.rmtree(old_dir, ignore_errors=True)
            except Exception as e:
                logger.error(f"Failed to move config dir {old_dir} -> {new_dir}: {e}")

        update_account_username(old_username, new_username)

    @classmethod
    async def fetch_quota(cls, username: str) -> Dict[str, Any]:
        """Runs `kaggle quota -v`, throttled globally and bounded by a timeout.

        The semaphore caps how many kaggle CLI processes can exist at once
        across the whole app; wait_for kills hung CLI calls so a stuck
        Kaggle API response can never leak a slot forever.
        """
        try:
            async with cls._get_quota_semaphore():
                return await asyncio.wait_for(
                    cls._fetch_quota_unthrottled(username),
                    timeout=cls.QUOTA_CALL_TIMEOUT_SECONDS
                )
        except asyncio.TimeoutError:
            logger.warning(
                f"kaggle quota timed out after {cls.QUOTA_CALL_TIMEOUT_SECONDS}s for {username}"
            )
            return {
                "raw": [],
                "gpu": {"name": "GPU (T4 x 2)", "used": 0, "limit": 30, "unit": "hours", "percent": 0},
                "tpu": {"name": "TPU VM v3-8", "used": 0, "limit": 20, "unit": "hours", "percent": 0},
                "error": f"quota lookup timed out after {cls.QUOTA_CALL_TIMEOUT_SECONDS}s"
            }

    @classmethod
    async def _fetch_quota_unthrottled(cls, username: str) -> Dict[str, Any]:
        """Runs `kaggle quota -v` and parses the output into structured quota info."""
        cli = get_kaggle_cli_path()
        cmd = [cli, "quota", "-v"]
        env = cls.get_account_env(username)
        
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env
            )
            stdout, stderr = await proc.communicate()
            out_str = stdout.decode("utf-8", errors="ignore")
            err_str = stderr.decode("utf-8", errors="ignore")
            
            if proc.returncode != 0:
                logger.warning(f"kaggle quota failed for {username} (rc={proc.returncode}): {err_str.strip()}")
            
            quotas = []
            if out_str:
                try:
                    csv_reader = csv.DictReader(io.StringIO(out_str))
                    for row in csv_reader:
                        quotas.append(row)
                except Exception as e:
                    logger.error(f"Failed to parse quota CSV for {username}: {e}")
            
            # Format summarized quota stats
            summary = {
                "raw": quotas,
                "gpu": {"name": "GPU (T4 x 2)", "used": 0, "limit": 30, "unit": "hours", "percent": 0},
                "tpu": {"name": "TPU VM v3-8", "used": 0, "limit": 20, "unit": "hours", "percent": 0}
            }

            def _hours(value_str) -> Optional[float]:
                """Parses quota hour strings like '12.50h' / '30.00 hours' / '0'."""
                if value_str is None:
                    return None
                cleaned = str(value_str).lower().replace("hours", "").replace("hour", "").replace("h", "").strip()
                try:
                    return float(cleaned)
                except ValueError:
                    return None

            for q in quotas:
                # New CLI schema: resource,used,remaining,total,refreshAt
                # Legacy schema:  acceleratorMaxHours / accelerator,used,limit
                acc_name = (q.get("resource") or q.get("accelerator") or "").lower()
                raw_used = q.get("used")
                limit_val = _hours(q.get("total")) if q.get("total") else _hours(q.get("limit"))
                used_val = _hours(raw_used)
                if used_val is None or limit_val is None or limit_val <= 0:
                    continue
                pct = round((used_val / limit_val) * 100, 1)
                entry = {
                    "name": q.get("resource") or q.get("accelerator") or "GPU",
                    "used": used_val,
                    "limit": limit_val,
                    "unit": "hours",
                    "percent": min(100.0, pct)
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
                "gpu": {"name": "GPU (T4 x 2)", "used": 0, "limit": 30, "unit": "hours", "percent": 0},
                "tpu": {"name": "TPU VM v3-8", "used": 0, "limit": 20, "unit": "hours", "percent": 0},
                "error": str(e)
            }

    @classmethod
    async def add_account(cls, api_key_or_token: str, custom_username: Optional[str] = None) -> Dict[str, Any]:
        """Registers a new Kaggle account, discovers its username, writes configs and saves to DB.

        Idempotent: if the exact same key is already registered (and no custom username
        is requested), the existing account is reused instead of creating a duplicate.
        """
        async with cls._lock:
            stripped_key = api_key_or_token.strip()

            # Reuse an existing registration for the same key unless renaming
            if not custom_username:
                existing = next(
                    (a for a in get_all_accounts() if a.get("api_key", "").strip() == stripped_key),
                    None
                )
                if existing:
                    username = existing["username"]
                    cls.setup_account_files(username, stripped_key)
                    return {
                        "id": existing["id"],
                        "username": username,
                        "quota": existing.get("last_quota"),
                        "existing": True
                    }

            temp_id = uuid.uuid4().hex[:8]
            if custom_username:
                username = custom_username.strip()
            else:
                username = await cls.fetch_username_for_key(temp_id, api_key_or_token)
                cls._cleanup_temp_dir(temp_id)

            # Setup config directory
            cls.setup_account_files(username, api_key_or_token)

            # Fetch quota
            quota_data = await cls.fetch_quota(username)

            # Save in database
            save_account(temp_id, username, api_key_or_token, quota_data)

            return {
                "id": temp_id,
                "username": username,
                "quota": quota_data
            }

    @classmethod
    async def initialize_from_env(cls):
        """Loads all API keys listed in .env KAGGLE_APIKEYS on startup."""
        if not KAGGLE_APIKEYS_RAW:
            return
        
        keys = [k.strip() for k in KAGGLE_APIKEYS_RAW.split(",") if k.strip()]
        for key in keys:
            try:
                await cls.add_account(key)
            except Exception as e:
                logger.error(f"Failed to auto-init account from .env: {e}")

    @classmethod
    async def refresh_all_quotas(cls) -> List[Dict[str, Any]]:
        """Refreshes quota data for all saved accounts (throttled, single-flight).

        Concurrent callers join the in-flight run rather than stacking a second
        batch of N kaggle CLI subprocesses on top of the first - that stacking
        is what used to OOM-kill the server with many accounts.
        """
        if cls._get_refresh_all_lock().locked():
            logger.info("Quota refresh already in progress - waiting for it instead of starting another.")
        async with cls._get_refresh_all_lock():
            accounts = get_all_accounts()
            results = await asyncio.gather(
                *(cls.refresh_account_quota(acc["username"]) for acc in accounts),
                return_exceptions=True
            )
            for acc, res in zip(accounts, results):
                if isinstance(res, Exception):
                    logger.warning(f"Quota refresh failed for @{acc['username']}: {res}")
            return get_all_accounts()

    @classmethod
    async def refresh_account_quota(cls, username: str) -> Dict[str, Any]:
        acc = get_account_by_username(username)
        if not acc:
            return {}
        cls.setup_account_files(username, acc["api_key"])
        quota = await cls.fetch_quota(username)
        save_account(acc["id"], username, acc["api_key"], quota)
        return quota

    @staticmethod
    def compute_gpu_runtime_budget_minutes(
        used_hours,
        limit_hours,
        concurrent_sessions: int
    ) -> Optional[int]:
        """Minutes a NEW GPU session may run before the account's remaining
        weekly GPU quota would kill it - or None when the quota is not the
        binding constraint (or cannot be trusted). Pure math, unit-testable.

        runway = remaining_quota / concurrent_sessions is how long the new
        kernel lasts before the quota is spent (every active GPU session burns
        quota while they overlap). Quota only binds when runway is UNDER
        Kaggle's 12h session cap - above that the session cap ends the run
        first (auto-stop publishes output), so no cap is returned.

        When it binds, budget = runway - (60 min pre-loop allowance + 10 min
        slop). The 60 min allowance matches what the batch scripts reserve for
        queue + installs + model load between session start and their
        inference loop (their 11h self-finish deadline is loop-anchored, while
        quota death is session-anchored), so the kernel's final flush/upload
        still lands BEFORE quota death even with a slow cold start. Because
        runway < 720 min here, budget < 650 min - always under the scripts'
        11h ceiling, never lengthening their default.

        Returns None (no cap) when:
        - remaining quota is already spent or unparsable,
        - runway >= 12h (the session cap, not the quota, ends the run),
        - budget < QUOTA_CAP_MIN_INJECT_MINUTES (a session this short can't
          beat model load; the monitor's bounded-loss stub path still applies).
        Never caps on a guess - unknown quota means leave the kernel alone.
        """
        try:
            used = float(used_hours)
            limit = float(limit_hours)
        except (TypeError, ValueError):
            return None
        if used < 0 or limit <= 0:
            return None
        remaining_h = limit - used
        if remaining_h <= 0:
            return None  # already spent - monitor's stub path handles it
        burners = max(1, int(concurrent_sessions or 1))
        runway_min = remaining_h * 60.0 / burners
        if runway_min >= SESSION_CAP_MINUTES:
            return None  # 12h session cap ends the run first - quota never binds
        budget_min = runway_min - PRE_LOOP_ALLOWANCE_MINUTES - FINISH_SLOP_MINUTES
        if budget_min < QUOTA_CAP_MIN_INJECT_MINUTES:
            return None  # too small to be useful after model load
        return int(budget_min)

    @classmethod
    async def gpu_runtime_budget_minutes(
        cls, username: str, concurrent_sessions: int
    ) -> Optional[int]:
        """Quota-aware MAX_RUNTIME_MINUTES for a NEW GPU kernel on `username`.

        Reads the account's stored last_quota, refreshing it LIVE first when the
        row is stale (no / old `last_checked`): the cap is only as good as the
        "remaining" number it is computed from, and a stale figure is how a
        capped kernel can still outrun the quota that actually applies. The
        refresh goes through the global quota semaphore (throttled), and a
        failed refresh falls back to the stored value rather than aborting the
        launch. Fresh rows (monitor keeps them ~5 min old during runs) never
        pay for a CLI call.
        """
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
                    dt = dt.replace(tzinfo=timezone.utc)
                age_min = (datetime.now(timezone.utc) - dt).total_seconds() / 60.0
            else:
                age_min = None  # legacy row, no timestamp -> treat as stale
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
            return None  # last lookup failed/timed out - do not trust it
        return cls.compute_gpu_runtime_budget_minutes(
            gpu.get("used"), gpu.get("limit"), concurrent_sessions
        )

    @classmethod
    def remove_account(cls, username: str):
        db_delete_account(username)
