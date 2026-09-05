import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from config import DB_PATH


def utcnow_iso() -> str:
    """Timezone-aware UTC timestamp (ISO-8601 with Z suffix, parseable by JS Date)."""
    return datetime.now(UTC).isoformat()


def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")

    # Accounts table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            id TEXT PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            api_key TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_quota TEXT,
            last_checked TEXT
        )
    """)

    # Runs history table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY,
            account_username TEXT NOT NULL,
            kernel_slug TEXT NOT NULL,
            kernel_ref TEXT NOT NULL,
            title TEXT NOT NULL,
            code_file TEXT,
            accelerator TEXT DEFAULT 'none',
            enable_internet INTEGER DEFAULT 1,
            is_trial INTEGER DEFAULT 0,
            timeout_seconds INTEGER DEFAULT 43200,
            status TEXT DEFAULT 'queued',
            status_message TEXT DEFAULT '',
            start_time TEXT NOT NULL,
            end_time TEXT,
            kaggle_url TEXT NOT NULL,
            workload_id TEXT,
            shard_index INTEGER,
            total_shards INTEGER,
            log_file TEXT,
            output_version INTEGER,
            telegram_notified_start INTEGER DEFAULT 0,
            telegram_notified_11h INTEGER DEFAULT 0,
            telegram_notified_12h INTEGER DEFAULT 0,
            telegram_notified_end INTEGER DEFAULT 0
        )
        """)

    # Lightweight migrations for pre-existing databases
    def _ensure_column(table: str, column_def: str):
        col_name = column_def.split()[0]
        cursor.execute(f"PRAGMA table_info({table})")
        existing = {row[1] for row in cursor.fetchall()}
        if col_name not in existing:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column_def}")

    _ensure_column("runs", "output_version INTEGER")
    # Repair rows written before enum-style CLI statuses were normalized
    cursor.execute(
        "UPDATE runs SET status = 'stopped' WHERE lower(status) LIKE '%cancel%'"
    )

    # Distributed workloads table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS distributed_workloads (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            workload_type TEXT NOT NULL,
            total_units INTEGER NOT NULL,
            accounts_used TEXT NOT NULL,
            created_at TEXT NOT NULL,
            status TEXT DEFAULT 'running'
        )
    """)

    # System settings table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)

    conn.commit()
    conn.close()


# Database helper functions
def save_account(
    account_id: str, username: str, api_key: str, last_quota: dict | None = None
):
    conn = get_db_connection()
    now = utcnow_iso()
    quota_str = json.dumps(last_quota) if last_quota else None
    with conn:
        conn.execute(
            """
            INSERT INTO accounts (id, username, api_key, created_at, last_quota, last_checked)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(username) DO UPDATE SET
                api_key = excluded.api_key,
                last_quota = excluded.last_quota,
                last_checked = excluded.last_checked
        """,
            (account_id, username, api_key, now, quota_str, now),
        )
    conn.close()


def get_all_accounts() -> list[dict[str, Any]]:
    conn = get_db_connection()
    rows = conn.execute("SELECT * FROM accounts ORDER BY username ASC").fetchall()
    accounts = []
    for r in rows:
        acc = dict(r)
        if acc["last_quota"]:
            try:
                acc["last_quota"] = json.loads(acc["last_quota"])
            except Exception:
                pass
        accounts.append(acc)
    conn.close()
    return accounts


def get_account_by_username(username: str) -> dict[str, Any] | None:
    conn = get_db_connection()
    row = conn.execute(
        "SELECT * FROM accounts WHERE username = ?", (username,)
    ).fetchone()
    conn.close()
    if row:
        acc = dict(row)
        if acc["last_quota"]:
            try:
                acc["last_quota"] = json.loads(acc["last_quota"])
            except Exception:
                pass
        return acc
    return None


def delete_account(username: str):
    conn = get_db_connection()
    with conn:
        conn.execute("DELETE FROM accounts WHERE username = ?", (username,))
    conn.close()


def update_account_username(old_username: str, new_username: str):
    conn = get_db_connection()
    with conn:
        conn.execute(
            "UPDATE accounts SET username = ? WHERE username = ?",
            (new_username, old_username),
        )
        conn.execute(
            "UPDATE runs SET account_username = ? WHERE account_username = ?",
            (new_username, old_username),
        )
    conn.close()


def create_run_record(run_data: dict[str, Any]):
    conn = get_db_connection()
    with conn:
        conn.execute(
            """
            INSERT INTO runs (
                id, account_username, kernel_slug, kernel_ref, title, code_file,
                accelerator, enable_internet, is_trial, timeout_seconds, status,
                status_message, start_time, kaggle_url, workload_id,
                shard_index, total_shards, log_file
            ) VALUES (
                :id, :account_username, :kernel_slug, :kernel_ref, :title, :code_file,
                :accelerator, :enable_internet, :is_trial, :timeout_seconds, :status,
                :status_message, :start_time, :kaggle_url, :workload_id,
                :shard_index, :total_shards, :log_file
            )
        """,
            run_data,
        )
    conn.close()


def update_run_status(
    run_id: str, status: str, status_message: str = "", end_time: str | None = None
):
    conn = get_db_connection()
    with conn:
        if end_time:
            conn.execute(
                """
                UPDATE runs SET status = ?, status_message = ?, end_time = ? WHERE id = ?
            """,
                (status, status_message, end_time, run_id),
            )
        else:
            conn.execute(
                """
                UPDATE runs SET status = ?, status_message = ? WHERE id = ?
            """,
                (status, status_message, run_id),
            )
    conn.close()


def update_run_telegram_flag(run_id: str, flag_name: str, value: int = 1):
    allowed_flags = {
        "telegram_notified_start",
        "telegram_notified_11h",
        "telegram_notified_12h",
        "telegram_notified_end",
    }
    if flag_name not in allowed_flags:
        raise ValueError(f"Invalid flag name: {flag_name}")
    conn = get_db_connection()
    with conn:
        conn.execute(f"UPDATE runs SET {flag_name} = ? WHERE id = ?", (value, run_id))
    conn.close()


def set_run_output_version(run_id: str, version: int):
    """Pins which Kaggle VERSION holds this run's real output.

    After a stop, the latest kernel version is our exit-stub (log only);
    the cancelled run version - with the partial /kaggle/working data -
    sits one behind. Pinning lets every later pull skip the stub.
    """
    conn = get_db_connection()
    with conn:
        conn.execute(
            "UPDATE runs SET output_version = ? WHERE id = ?", (int(version), run_id)
        )
    conn.close()


def get_all_runs(limit: int = 100) -> list[dict[str, Any]]:
    conn = get_db_connection()
    rows = conn.execute(
        "SELECT * FROM runs ORDER BY start_time DESC LIMIT ?", (limit,)
    ).fetchall()
    runs = [dict(r) for r in rows]
    conn.close()
    return runs


def get_active_runs() -> list[dict[str, Any]]:
    conn = get_db_connection()
    rows = conn.execute(
        "SELECT * FROM runs WHERE status IN ('queued', 'running') ORDER BY start_time DESC"
    ).fetchall()
    runs = [dict(r) for r in rows]
    conn.close()
    return runs


def get_run_by_id(run_id: str) -> dict[str, Any] | None:
    conn = get_db_connection()
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def create_distributed_workload(workload_data: dict[str, Any]):
    conn = get_db_connection()
    with conn:
        conn.execute(
            """
            INSERT INTO distributed_workloads (id, title, workload_type, total_units, accounts_used, created_at, status)
            VALUES (:id, :title, :workload_type, :total_units, :accounts_used, :created_at, :status)
        """,
            workload_data,
        )
    conn.close()


def get_all_workloads() -> list[dict[str, Any]]:
    conn = get_db_connection()
    rows = conn.execute(
        "SELECT * FROM distributed_workloads ORDER BY created_at DESC"
    ).fetchall()
    workloads = []
    for r in rows:
        w = dict(r)
        try:
            w["accounts_used"] = json.loads(w["accounts_used"])
        except Exception:
            pass
        workloads.append(w)
    conn.close()
    return workloads


def update_workload_status(workload_id: str, status: str):
    conn = get_db_connection()
    with conn:
        conn.execute(
            "UPDATE distributed_workloads SET status = ? WHERE id = ?",
            (status, workload_id),
        )
    conn.close()


def get_setting(key: str, default: str | None = None) -> str | None:
    conn = get_db_connection()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key: str, value: str):
    conn = get_db_connection()
    with conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
    conn.close()
