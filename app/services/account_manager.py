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
from typing import Dict, List, Any, Optional
from app.config import ACCOUNTS_DIR, KAGGLE_APIKEYS_RAW, get_kaggle_cli_path
from app.database import (
    save_account, get_all_accounts, get_account_by_username,
    delete_account as db_delete_account
)

logger = logging.getLogger("account_manager")

class AccountManager:
    _lock = asyncio.Lock()

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

    @classmethod
    def get_account_env(cls, username: str) -> Dict[str, str]:
        """Builds subprocess environment variables for the specified account."""
        acc_dir = cls.get_account_config_dir(username)
        env = os.environ.copy()
        env["KAGGLE_CONFIG_DIR"] = str(acc_dir)
        
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

            for q in quotas:
                acc_name = q.get("accelerator", "").lower()
                used_str = q.get("used", "0").replace("hours", "").strip()
                limit_str = q.get("limit", "30").replace("hours", "").strip()
                try:
                    used_val = float(used_str)
                    limit_val = float(limit_str)
                    pct = round((used_val / limit_val) * 100, 1) if limit_val > 0 else 0
                    if "tpu" in acc_name:
                        summary["tpu"] = {
                            "name": q.get("accelerator", "TPU"),
                            "used": used_val,
                            "limit": limit_val,
                            "unit": "hours",
                            "percent": min(100.0, pct)
                        }
                    else:
                        summary["gpu"] = {
                            "name": q.get("accelerator", "GPU"),
                            "used": used_val,
                            "limit": limit_val,
                            "unit": "hours",
                            "percent": min(100.0, pct)
                        }
                except Exception:
                    pass

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
        """Refreshes quota data for all saved accounts."""
        accounts = get_all_accounts()
        tasks = []
        for acc in accounts:
            tasks.append(cls.refresh_account_quota(acc["username"]))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
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

    @classmethod
    def remove_account(cls, username: str):
        db_delete_account(username)
