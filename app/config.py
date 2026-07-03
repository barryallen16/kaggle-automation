import os
import sys
import shutil
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Base directories
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
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
KAGGLE_CLI_PATH = get_kaggle_cli_path()

# Session limits (in seconds)
MAX_KAGGLE_SESSION_SECONDS = 12 * 3600  # 12 hours
WARNING_BEFORE_EXPIRY_SECONDS = 3600   # 1 hour warning
TRIAL_RUN_DEFAULT_TIMEOUT = 300        # 5 minutes
