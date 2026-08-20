import os
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

# Application Config
KAGGLE_APIKEYS_RAW = os.getenv("KAGGLE_APIKEYS", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
KAGGLE_CLI_PATH = os.getenv("KAGGLE_CLI_PATH", "kaggle")

# Session limits (in seconds)
MAX_KAGGLE_SESSION_SECONDS = 12 * 3600  # 12 hours
WARNING_BEFORE_EXPIRY_SECONDS = 3600   # 1 hour warning
TRIAL_RUN_DEFAULT_TIMEOUT = 300        # 5 minutes
