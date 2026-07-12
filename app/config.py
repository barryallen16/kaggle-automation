import os
import sys
import shutil
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Base directories
BASE_DIR = Path(__file__).resolve().parent.parent
# DATA_DIR can be redirected via env (used by the test suite for isolation).
# Empty or whitespace values are treated as unset (Path("") would silently become cwd).
_data_dir_env = (os.getenv("AUTOMATION_DATA_DIR") or "").strip()
DATA_DIR = Path(_data_dir_env).expanduser().resolve() if _data_dir_env else BASE_DIR / "data"
ACCOUNTS_DIR = DATA_DIR / "accounts"
NOTEBOOKS_DIR = DATA_DIR / "notebooks"
LOGS_DIR = DATA_DIR / "logs"
OUTPUTS_DIR = DATA_DIR / "outputs"
DB_PATH = DATA_DIR / "kaggle_automation.db"

# Ensure all required directories exist
for directory in [DATA_DIR, ACCOUNTS_DIR, NOTEBOOKS_DIR, LOGS_DIR, OUTPUTS_DIR]:
    directory.mkdir(parents=True, exist_ok=True)

def get_kaggle_cli_path() -> str:
    """Robustly discovers the kaggle CLI executable path across OS and virtual environments."""
    # 1. Check current running Python environment (e.g. .venv/Scripts on Windows, .venv/bin on Linux)
    is_windows = os.name == "nt"
    bin_folder = "Scripts" if is_windows else "bin"
    binary_name = "kaggle.exe" if is_windows else "kaggle"
    
    current_venv_binary = Path(sys.prefix) / bin_folder / binary_name
    if current_venv_binary.exists():
        return str(current_venv_binary)

    # 2. Check project root .venv directory
    project_venv_binary = BASE_DIR / ".venv" / bin_folder / binary_name
    if project_venv_binary.exists():
        return str(project_venv_binary)

    # 3. Check system PATH
    found = shutil.which(binary_name) or shutil.which("kaggle")
    if found:
        return found

    # 4. Fallback to env or default
    return os.getenv("KAGGLE_CLI_PATH", "kaggle")

# Application Config
KAGGLE_APIKEYS_RAW = os.getenv("KAGGLE_APIKEYS", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
# Dashboard login secret. Leave EMPTY to disable auth (local dev only).
APP_AUTH_TOKEN = os.getenv("APP_AUTH_TOKEN", "")
KAGGLE_CLI_PATH = get_kaggle_cli_path()

# Session limits (in seconds)
MAX_KAGGLE_SESSION_SECONDS = 12 * 3600  # 12 hours
WARNING_BEFORE_EXPIRY_SECONDS = 3600   # 1 hour warning
TRIAL_RUN_DEFAULT_TIMEOUT = 300        # 5 minutes

def get_kernel_env_defaults() -> dict:
    """Secrets to inject into every pushed kernel's environment.

    Read lazily (re-reading .env) so adding/updating keys takes effect on the
    NEXT launch without a server restart. Only HF_TOKEN today - extend here.
    A READ-scoped token suffices: kernels only download public artifacts.
    """
    try:
        from dotenv import load_dotenv
        load_dotenv()  # picks up newly added keys; never overrides existing env
    except Exception:
        pass
    out = {}
    hf_token = (os.getenv("HF_TOKEN") or "").strip()
    if hf_token:
        out["HF_TOKEN"] = hf_token
    return out
