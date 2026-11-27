import os
import shutil
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
_data_dir_env = (os.getenv("AUTOMATION_DATA_DIR") or "").strip()
DATA_DIR = (
    Path(_data_dir_env).expanduser().resolve() if _data_dir_env else BASE_DIR / "data"
)
ACCOUNTS_DIR = DATA_DIR / "accounts"
NOTEBOOKS_DIR = DATA_DIR / "notebooks"
LOGS_DIR = DATA_DIR / "logs"
OUTPUTS_DIR = DATA_DIR / "outputs"
DB_PATH = DATA_DIR / "kaggle_automation.db"

for directory in [DATA_DIR, ACCOUNTS_DIR, NOTEBOOKS_DIR, LOGS_DIR, OUTPUTS_DIR]:
    directory.mkdir(parents=True, exist_ok=True)


def get_kaggle_cli_path() -> str:
    is_windows = os.name == "nt"
    bin_folder = "Scripts" if is_windows else "bin"
    binary_name = "kaggle.exe" if is_windows else "kaggle"

    current_venv_binary = Path(sys.prefix) / bin_folder / binary_name
    if current_venv_binary.exists():
        return str(current_venv_binary)

    project_venv_binary = BASE_DIR / ".venv" / bin_folder / binary_name
    if project_venv_binary.exists():
        return str(project_venv_binary)

    found = shutil.which(binary_name) or shutil.which("kaggle")
    if found:
        return found

    return os.getenv("KAGGLE_CLI_PATH", "kaggle")


KAGGLE_APIKEYS_RAW = os.getenv("KAGGLE_APIKEYS", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_MODE = os.getenv("TELEGRAM_MODE", "errors-only")
APP_AUTH_TOKEN = os.getenv("APP_AUTH_TOKEN", "")
KAGGLE_CLI_PATH = get_kaggle_cli_path()

MAX_KAGGLE_SESSION_SECONDS = 12 * 3600
WARNING_BEFORE_EXPIRY_SECONDS = 3600
TRIAL_RUN_DEFAULT_TIMEOUT = 300

KAGGLE_MAX_GPU_SESSIONS_PER_ACCOUNT = 2


def is_gpu_accelerator(accelerator: object) -> bool:
    a = str(accelerator or "").lower()
    return bool(a) and a not in ("none", "default", "cpu")


def get_kernel_env_defaults() -> dict:
    try:
        load_dotenv()
    except Exception:
        pass
    out = {}
    hf_token = (os.getenv("HF_TOKEN") or "").strip()
    if hf_token:
        out["HF_TOKEN"] = hf_token
    wandb_key = (os.getenv("WANDB_API_KEY") or "").strip()
    if wandb_key:
        out["WANDB_API_KEY"] = wandb_key
    wandb_project = (os.getenv("WANDB_PROJECT") or "").strip()
    if wandb_project:
        out["WANDB_PROJECT"] = wandb_project
    return out
