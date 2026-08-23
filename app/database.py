import sqlite3
import json
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from app.config import DB_PATH

def utcnow_iso() -> str:
    """Timezone-aware UTC timestamp (ISO-8601 with Z suffix, parseable by JS Date)."""
    return datetime.now(timezone.utc).isoformat()

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
            telegram_notified_start INTEGER DEFAULT 0,
            telegram_notified_11h INTEGER DEFAULT 0,
            telegram_notified_12h INTEGER DEFAULT 0,
            telegram_notified_end INTEGER DEFAULT 0
        )
    """)
    
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
def save_account(account_id: str, username: str, api_key: str, last_quota: Optional[Dict] = None):
    conn = get_db_connection()
    now = utcnow_iso()
    quota_str = json.dumps(last_quota) if last_quota else None
    with conn:
        conn.execute("""
            INSERT INTO accounts (id, username, api_key, created_at, last_quota, last_checked)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(username) DO UPDATE SET
                api_key = excluded.api_key,
                last_quota = excluded.last_quota,
                last_checked = excluded.last_checked
        """, (account_id, username, api_key, now, quota_str, now))
    conn.close()

def get_all_accounts() -> List[Dict[str, Any]]:
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

def get_account_by_username(username: str) -> Optional[Dict[str, Any]]:
    conn = get_db_connection()
    row = conn.execute("SELECT * FROM accounts WHERE username = ?", (username,)).fetchone()
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
        conn.execute("UPDATE accounts SET username = ? WHERE username = ?", (new_username, old_username))
        conn.execute("UPDATE runs SET account_username = ? WHERE account_username = ?", (new_username, old_username))
    conn.close()

def create_run_record(run_data: Dict[str, Any]):
    conn = get_db_connection()
    with conn:
        conn.execute("""
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
        """, run_data)
    conn.close()

def update_run_status(run_id: str, status: str, status_message: str = "", end_time: Optional[str] = None):
    conn = get_db_connection()
    with conn:
        if end_time:
            conn.execute("""
                UPDATE runs SET status = ?, status_message = ?, end_time = ? WHERE id = ?
            """, (status, status_message, end_time, run_id))
        else:
            conn.execute("""
                UPDATE runs SET status = ?, status_message = ? WHERE id = ?
            """, (status, status_message, run_id))
    conn.close()

def update_run_telegram_flag(run_id: str, flag_name: str, value: int = 1):
    allowed_flags = {"telegram_notified_start", "telegram_notified_11h", "telegram_notified_12h", "telegram_notified_end"}
    if flag_name not in allowed_flags:
        raise ValueError(f"Invalid flag name: {flag_name}")
    conn = get_db_connection()
    with conn:
        conn.execute(f"UPDATE runs SET {flag_name} = ? WHERE id = ?", (value, run_id))
    conn.close()

def get_all_runs(limit: int = 100) -> List[Dict[str, Any]]:
    conn = get_db_connection()
    rows = conn.execute("SELECT * FROM runs ORDER BY start_time DESC LIMIT ?", (limit,)).fetchall()
    runs = [dict(r) for r in rows]
    conn.close()
    return runs

def get_active_runs() -> List[Dict[str, Any]]:
    conn = get_db_connection()
    rows = conn.execute("SELECT * FROM runs WHERE status IN ('queued', 'running') ORDER BY start_time DESC").fetchall()
    runs = [dict(r) for r in rows]
    conn.close()
    return runs

def get_run_by_id(run_id: str) -> Optional[Dict[str, Any]]:
    conn = get_db_connection()
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    conn.close()
    return dict(row) if row else None

def create_distributed_workload(workload_data: Dict[str, Any]):
    conn = get_db_connection()
    with conn:
        conn.execute("""
            INSERT INTO distributed_workloads (id, title, workload_type, total_units, accounts_used, created_at, status)
            VALUES (:id, :title, :workload_type, :total_units, :accounts_used, :created_at, :status)
        """, workload_data)
    conn.close()

def get_all_workloads() -> List[Dict[str, Any]]:
    conn = get_db_connection()
    rows = conn.execute("SELECT * FROM distributed_workloads ORDER BY created_at DESC").fetchall()
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
        conn.execute("UPDATE distributed_workloads SET status = ? WHERE id = ?", (status, workload_id))
    conn.close()

def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    conn = get_db_connection()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default

def set_setting(key: str, value: str):
    conn = get_db_connection()
    with conn:
        conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
    conn.close()
