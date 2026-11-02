#!/usr/bin/env python3
"""CLI helper to manage and launch runs on kabila-aws.isroot.in (or localhost).

Built to Agent eXperience Interface (AXI) standards:
- TOON token-efficient output boundary (~40% token savings).
- Content-first no-args home view (live dashboard without extra calls).
- Structured errors on stdout with actionable hints and exit codes 0/1/2.
- Pre-computed aggregates (free GPU slots, active run counts).
"""

import argparse
import hashlib
import hmac
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from axi import AxiParser, bin_path, emit, fail, print_hints, usage_error

VERSION = "0.2.0"
DEFAULT_URL = "https://kabila-aws.isroot.in"
SESSION_COOKIE_NAME = "kaggle_auto_session"
SESSION_TTL_SECONDS = 7 * 24 * 3600

# AXI Fast path for version
if len(sys.argv) == 2 and sys.argv[1] in ("-v", "-V", "--version"):
    print(VERSION)
    sys.exit(0)


def _find_env_file(start_dir: Path | None = None) -> Path | None:
    current = (start_dir or Path.cwd()).resolve()
    for directory in [current, *current.parents]:
        cand = directory / ".env"
        if cand.is_file():
            return cand
    return None


def _parse_env(path: Path) -> dict[str, str]:
    env = {}
    if not path.is_file():
        return env
    try:
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            key = k.strip()
            val = v.strip().strip("\"'")
            env[key] = val
    except Exception:
        pass
    return env


def load_config(explicit_url: str | None = None, explicit_token: str | None = None) -> tuple[str, str]:
    env_file = _find_env_file()
    file_env = _parse_env(env_file) if env_file else {}

    url = (
        explicit_url
        or os.environ.get("KABILA_API_URL")
        or file_env.get("KABILA_API_URL")
        or DEFAULT_URL
    ).strip().rstrip("/")

    token = (
        explicit_token
        or os.environ.get("KABILA_APP_AUTH_TOKEN")
        or os.environ.get("APP_AUTH_TOKEN")
        or file_env.get("KABILA_APP_AUTH_TOKEN")
        or file_env.get("APP_AUTH_TOKEN")
        or ""
    ).strip().strip("\"'")

    return url, token


def make_session_cookie(token: str, now: float | None = None) -> str:
    if not token:
        return ""
    expiry = str(int((now or time.time()) + SESSION_TTL_SECONDS))
    key = hashlib.sha256(token.encode("utf-8")).digest()
    sig = hmac.new(key, expiry.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{expiry}.{sig}"


def _build_multipart(fields: dict[str, Any], files: dict[str, tuple[str, bytes]]) -> tuple[bytes, str]:
    boundary = f"----KabilaFormBoundary{uuid.uuid4().hex}"
    parts: list[bytes] = []

    for name, val in fields.items():
        if val is None:
            continue
        str_val = str(val).lower() if isinstance(val, bool) else str(val)
        header = f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
        parts.append(header.encode() + str_val.encode() + b"\r\n")

    for name, (filename, content) in files.items():
        header = (
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"{name}\"; filename=\"{filename}\"\r\n"
            f"Content-Type: application/octet-stream\r\n\r\n"
        )
        parts.append(header.encode() + content + b"\r\n")

    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def api_request(
    url: str,
    endpoint: str,
    token: str = "",
    method: str = "GET",
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 45,
) -> dict[str, Any] | str:
    full_url = f"{url.rstrip('/')}/{endpoint.lstrip('/')}"
    req_headers = {
        "User-Agent": f"kaggle-runner/{VERSION}",
        "Accept": "application/json, text/plain, */*",
    }
    if headers:
        req_headers.update(headers)

    cookie = make_session_cookie(token)
    if cookie:
        req_headers["Cookie"] = f"{SESSION_COOKIE_NAME}={cookie}"

    req = urllib.request.Request(full_url, data=data, headers=req_headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content_type = resp.headers.get("Content-Type", "")
            raw = resp.read()
            if "application/json" in content_type:
                return json.loads(raw.decode("utf-8", errors="replace"))
            try:
                return json.loads(raw.decode("utf-8", errors="replace"))
            except Exception:
                return raw.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raw_err = e.read().decode("utf-8", errors="replace")
        detail = raw_err
        try:
            parsed = json.loads(raw_err)
            detail = parsed.get("detail") or parsed.get("error") or raw_err
        except Exception:
            pass
        if e.code == 401:
            return {
                "error": f"Authentication failed (HTTP 401): {detail}",
                "http_status": 401,
                "help": "Pass --token <key> or run `kaggle_runner auth --token <key>`",
            }
        return {"error": f"HTTP {e.code}: {detail}", "http_status": e.code, "help": f"Check request parameters for {endpoint}"}
    except urllib.error.URLError as e:
        return {
            "error": f"Cannot reach server at {url}: {e.reason}",
            "help": "Verify network access or specify --url http://127.0.0.1:8000 for local dev",
        }
    except Exception as e:
        return {"error": str(e), "help": "Unexpected request failure"}


def _aggregate_accounts(accounts_data: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    summary = []
    total_free = 0
    for acc in accounts_data:
        u = acc.get("username", "unknown")
        busy = acc.get("active_gpu_runs", len(acc.get("active_runs", [])))
        gpu_left = acc.get("remaining_gpu_hours", "N/A")
        free = max(0, 2 - busy)
        total_free += free
        summary.append({
            "username": u,
            "free_slots": free,
            "busy_sessions": busy,
            "quota_hours": gpu_left,
        })
    return summary, total_free


def cmd_home(url: str, token: str) -> int:
    """AXI §8: Content-first home view displayed on no-args execution."""
    health = api_request(url, "/api/health", token=token)
    if isinstance(health, dict) and health.get("error"):
        fail(health["error"], hint=health.get("help"))

    accounts_resp = api_request(url, "/api/accounts", token=token)
    active_resp = api_request(url, "/api/runs/active", token=token)

    accounts_data = accounts_resp.get("accounts", []) if isinstance(accounts_resp, dict) else []
    active_runs = active_resp.get("active_runs", []) if isinstance(active_resp, dict) else []

    accounts_summary, total_free = _aggregate_accounts(accounts_data)

    output: dict[str, Any] = {
        "bin": bin_path(),
        "description": "Kaggle batch automation runner and monitor at kabila-aws.isroot.in",
        "url": url,
        "status": "ok",
        "total_free_gpu_slots": total_free,
        "accounts": accounts_summary,
        "active_runs_count": len(active_runs),
    }

    if active_runs:
        output["active_runs"] = [
            {
                "id": r.get("id"),
                "title": r.get("title"),
                "account": r.get("account_username"),
                "status": r.get("status"),
                "accelerator": r.get("accelerator"),
            }
            for r in active_runs[:10]
        ]

    hints = [
        "Run `kaggle_runner run <file> --title <title>` to launch a kernel",
        "Run `kaggle_runner distributed <file> --title <title>` to shard across accounts",
        "Run `kaggle_runner runs --active` to inspect active runs",
        "Run `kaggle_runner --help` for full command reference",
    ]
    emit(output, hints=hints)
    return 0


def cmd_auth(args: argparse.Namespace) -> int:
    token = (args.token or "").strip().strip("\"'")
    if not token:
        usage_error("Missing token argument", hint="Pass --token <APP_AUTH_TOKEN>")

    cwd = Path.cwd().resolve()
    gitignore_path = cwd / ".gitignore"
    env_path = cwd / ".env"

    # 1. Protect secrets: ensure .gitignore exists and ignores .env
    if gitignore_path.exists():
        gi_text = gitignore_path.read_text(encoding="utf-8")
        if not re.search(r"^\.env(\b|\.|\*)", gi_text, re.MULTILINE):
            with open(gitignore_path, "a", encoding="utf-8") as f:
                f.write("\n# Secrets\n.env\n.env.*\n")
    else:
        gitignore_path.write_text("# Secrets\n.env\n.env.*\n", encoding="utf-8")

    # 2. Check if already configured (AXI §6 idempotent mutation)
    url = (args.url or DEFAULT_URL).strip().rstrip("/")
    existing_env = _parse_env(env_path) if env_path.exists() else {}
    if existing_env.get("KABILA_APP_AUTH_TOKEN") == token and existing_env.get("KABILA_API_URL") == url:
        emit({
            "status": "already configured (no-op)",
            "url": url,
            "saved_to": str(env_path),
            "gitignored": True,
        }, hints=["Run `kaggle_runner status` to verify slot availability."])
        return 0

    lines: list[str] = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()

    updated_url = False
    updated_token = False
    new_lines = []
    for line in lines:
        if line.strip().startswith("KABILA_API_URL="):
            new_lines.append(f"KABILA_API_URL={url}")
            updated_url = True
        elif line.strip().startswith("KABILA_APP_AUTH_TOKEN="):
            new_lines.append(f"KABILA_APP_AUTH_TOKEN={token}")
            updated_token = True
        else:
            new_lines.append(line)

    if not updated_url:
        new_lines.append(f"KABILA_API_URL={url}")
    if not updated_token:
        new_lines.append(f"KABILA_APP_AUTH_TOKEN={token}")

    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

    # 3. Test credentials against server
    health = api_request(url, "/api/health", token=token)
    if isinstance(health, dict) and health.get("error"):
        fail(health["error"], hint=health.get("help"))

    accounts_resp = api_request(url, "/api/accounts", token=token)
    accounts = []
    if isinstance(accounts_resp, dict) and accounts_resp.get("success"):
        accounts = [a.get("username") for a in accounts_resp.get("accounts", [])]

    emit({
        "status": "authenticated",
        "url": url,
        "saved_to": str(env_path),
        "gitignored": True,
        "available_accounts": accounts,
    }, hints=["Run `kaggle_runner status` to check live GPU slots."])
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    url, token = load_config(args.url, args.token)
    health = api_request(url, "/api/health", token=token)
    if isinstance(health, dict) and health.get("error"):
        fail(health["error"], hint=health.get("help"))

    accounts_resp = api_request(url, "/api/accounts", token=token)
    active_resp = api_request(url, "/api/runs/active", token=token)

    accounts_data = accounts_resp.get("accounts", []) if isinstance(accounts_resp, dict) else []
    active_runs = active_resp.get("active_runs", []) if isinstance(active_resp, dict) else []

    accounts_summary, total_free = _aggregate_accounts(accounts_data)

    report = {
        "url": url,
        "total_free_gpu_slots": total_free,
        "accounts": accounts_summary,
        "active_runs_count": len(active_runs),
    }
    if active_runs:
        report["active_runs"] = [
            {
                "id": r.get("id"),
                "title": r.get("title"),
                "account": r.get("account_username"),
                "status": r.get("status"),
                "accelerator": r.get("accelerator"),
            }
            for r in active_runs[:10]
        ]
    else:
        report["active_runs"] = "0 active runs"

    hints = [
        "Run `kaggle_runner run <file> --title <title>` to launch a job" if total_free > 0 else "All GPU slots busy; wait for active runs to finish",
        "Run `kaggle_runner runs --active` to list in-flight runs",
    ]
    emit(report, hints=hints)
    return 0


def cmd_accounts(args: argparse.Namespace) -> int:
    url, token = load_config(args.url, args.token)
    resp = api_request(url, "/api/accounts", token=token)
    if isinstance(resp, dict) and resp.get("error"):
        fail(resp["error"], hint=resp.get("help"))

    accounts_data = resp.get("accounts", []) if isinstance(resp, dict) else []
    if not accounts_data:
        emit({"accounts": "0 registered Kaggle accounts"}, hints=["Add accounts in server .env via KAGGLE_APIKEYS"])
        return 0

    accounts_summary, total_free = _aggregate_accounts(accounts_data)
    emit({
        "total_accounts": len(accounts_summary),
        "total_free_gpu_slots": total_free,
        "accounts": accounts_summary,
    }, hints=["Run `kaggle_runner run <file>` to dispatch to the first free slot"])
    return 0


def cmd_runs(args: argparse.Namespace) -> int:
    url, token = load_config(args.url, args.token)
    endpoint = "/api/runs/active" if args.active else f"/api/runs?limit={args.limit}"
    resp = api_request(url, endpoint, token=token)
    if isinstance(resp, dict) and resp.get("error"):
        fail(resp["error"], hint=resp.get("help"))

    runs_data = resp.get("active_runs" if args.active else "runs", []) if isinstance(resp, dict) else []
    if not runs_data:
        label = "active" if args.active else "recent"
        emit({f"{label}_runs": f"0 {label} runs found"}, hints=["Run `kaggle_runner run <file>` to launch a kernel"])
        return 0

    # Minimal schema (AXI §2)
    fields = [f.strip() for f in args.fields.split(",")] if args.fields else ["id", "title", "account", "accelerator", "status"]
    filtered = []
    for r in runs_data:
        row = {}
        for f in fields:
            val = r.get("account_username" if f == "account" else f)
            row[f] = val if val is not None else ""
        filtered.append(row)

    total_str = f"count: {len(filtered)}" + (f" of {resp.get('total')}" if resp.get("total") else "")
    first_id = runs_data[0].get("id")
    hints = [
        f"Run `kaggle_runner logs {first_id}` to view logs" if first_id else "",
        f"Run `kaggle_runner stop {first_id}` to terminate" if first_id else "",
    ]
    emit({total_str: None, "runs": filtered}, hints=hints)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    url, token = load_config(args.url, args.token)
    file_path = Path(args.file).resolve()
    if not file_path.is_file():
        usage_error(f"File not found: {file_path}", hint="Provide a valid .py or .ipynb file path")

    account = args.account
    if not account:
        acc_resp = api_request(url, "/api/accounts", token=token)
        if isinstance(acc_resp, dict) and acc_resp.get("accounts"):
            for a in acc_resp["accounts"]:
                busy = a.get("active_gpu_runs", len(a.get("active_runs", [])))
                if busy < 2:
                    account = a.get("username")
                    break
        if not account:
            fail(
                "No Kaggle account has a free GPU slot",
                hint="Pass --account <username> explicitly or run `kaggle_runner status` to see busy sessions",
            )

    title = args.title or file_path.stem
    content = file_path.read_bytes()

    fields = {
        "account_username": account,
        "title": title,
        "accelerator": args.accelerator,
        "enable_internet": not args.no_internet,
        "is_trial": args.trial,
    }
    if args.timeout:
        fields["timeout_seconds"] = args.timeout

    files = {"file": (file_path.name, content)}
    body, content_type = _build_multipart(fields, files)

    resp = api_request(
        url,
        "/api/runs/upload-and-launch",
        token=token,
        method="POST",
        data=body,
        headers={"Content-Type": content_type},
        timeout=180,
    )

    if isinstance(resp, dict) and resp.get("error"):
        fail(resp["error"], hint=resp.get("help"))

    run_id = resp.get("run_id") or resp.get("id") if isinstance(resp, dict) else None
    hints = [
        f"Run `kaggle_runner logs {run_id}` to follow logs" if run_id else "",
        f"Run `kaggle_runner stop {run_id}` to abort" if run_id else "",
    ]
    emit(resp, hints=hints)
    return 0


def cmd_distributed(args: argparse.Namespace) -> int:
    url, token = load_config(args.url, args.token)
    file_path = Path(args.file).resolve()
    if not file_path.is_file():
        usage_error(f"File not found: {file_path}", hint="Provide a valid .py or .ipynb file path")

    accounts_raw = args.accounts
    if not accounts_raw:
        acc_resp = api_request(url, "/api/accounts", token=token)
        if isinstance(acc_resp, dict) and acc_resp.get("accounts"):
            accounts_raw = ",".join(a.get("username") for a in acc_resp["accounts"])
        if not accounts_raw:
            fail("No Kaggle accounts discovered", hint="Pass --accounts acc1,acc2")

    title = args.title or file_path.stem
    content = file_path.read_bytes()

    fields = {
        "base_title": title,
        "accounts": accounts_raw,
        "total_items": args.total_items,
        "start_offset": args.start_offset,
        "accelerator": args.accelerator,
        "enable_internet": not args.no_internet,
        "is_trial": args.trial,
        "sessions_per_account": str(args.sessions_per_account),
    }
    if args.timeout:
        fields["timeout_seconds"] = args.timeout

    files = {"file": (file_path.name, content)}
    body, content_type = _build_multipart(fields, files)

    resp = api_request(
        url,
        "/api/distributed/upload-and-launch",
        token=token,
        method="POST",
        data=body,
        headers={"Content-Type": content_type},
        timeout=240,
    )

    if isinstance(resp, dict) and resp.get("error"):
        fail(resp["error"], hint=resp.get("help"))

    w_id = resp.get("workload_id") if isinstance(resp, dict) else None
    hints = [
        f"Run `kaggle_runner stop {w_id} --workload` to abort all shards" if w_id else "",
        "Run `kaggle_runner runs --active` to view active shard progress",
    ]
    emit(resp, hints=hints)
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    url, token = load_config(args.url, args.token)
    endpoint = f"/api/runs/{args.run_id}/logs?fetch_remote={'true' if args.remote else 'false'}"
    resp = api_request(url, endpoint, token=token)

    if isinstance(resp, dict) and resp.get("error"):
        fail(resp["error"], hint=resp.get("help"))

    text = str(resp)
    lines = text.splitlines()
    total_lines = len(lines)

    # Content truncation (AXI §3)
    if not args.full and total_lines > args.tail:
        shown_lines = lines[-args.tail:]
        print("\n".join(shown_lines))
        print(f"\n... (truncated, {total_lines} lines total)")
        print_hints([f"Run `kaggle_runner logs {args.run_id} --full` to see complete output"])
    else:
        print(text if text.strip() else "(no log output yet)")

    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    url, token = load_config(args.url, args.token)
    if args.workload:
        endpoint = f"/api/distributed/{args.target_id}/stop"
    else:
        endpoint = f"/api/runs/{args.target_id}/stop"

    resp = api_request(url, endpoint, token=token, method="POST")
    if isinstance(resp, dict) and resp.get("error"):
        fail(resp["error"], hint=resp.get("help"))

    emit(resp, hints=["Run `kaggle_runner runs --active` to verify session release"])
    return 0


def main() -> int:
    url, token = load_config()

    # AXI §8: If run without arguments, display live home view
    if len(sys.argv) == 1:
        return cmd_home(url, token)

    parser = AxiParser(
        prog="kaggle_runner",
        description="Interact with Kaggle batch automation runner (kabila-aws.isroot.in)",
    )
    parser.add_argument("-v", "-V", "--version", action="version", version=VERSION)
    parser.add_argument("--url", help="API URL (default: KABILA_API_URL or https://kabila-aws.isroot.in)")
    parser.add_argument("--token", help="APP_AUTH_TOKEN secret")

    sub = parser.add_subparsers(dest="cmd", required=True)

    # auth
    p_auth = sub.add_parser("auth", help="Save token in .env and ensure .env is gitignored")
    p_auth.add_argument("--token", "-t", required=True, help="APP_AUTH_TOKEN secret")
    p_auth.add_argument("--url", "-u", default=DEFAULT_URL, help=f"Server URL (default: {DEFAULT_URL})")

    # status
    sub.add_parser("status", help="Check server health, accounts, free GPU slots, and active runs")

    # accounts
    sub.add_parser("accounts", help="List registered Kaggle accounts and quota info")

    # runs
    p_runs = sub.add_parser("runs", help="List runs")
    p_runs.add_argument("--active", action="store_true", help="List active runs only")
    p_runs.add_argument("--limit", type=int, default=20, help="Max runs to display")
    p_runs.add_argument("--fields", help="Comma-separated field list (e.g. id,title,status,account)")

    # run
    p_run = sub.add_parser("run", help="Upload and launch a single kernel run")
    p_run.add_argument("file", help="Python script (.py) or notebook (.ipynb)")
    p_run.add_argument("--title", "-t", help="Kernel title (max 50 chars)")
    p_run.add_argument("--account", "-a", help="Kaggle account username")
    p_run.add_argument(
        "--accelerator",
        "-g",
        "--gpu",
        default="nvidia-tesla-t4-x2",
        choices=["nvidia-tesla-t4-x2", "nvidia-tesla-t4", "v3-8", "none"],
        help="Hardware accelerator",
    )
    p_run.add_argument("--no-internet", action="store_true", help="Disable internet access")
    p_run.add_argument("--trial", action="store_true", help="Run 5-minute preflight trial")
    p_run.add_argument("--timeout", type=int, help="Execution timeout in seconds")

    # distributed
    p_dist = sub.add_parser("distributed", help="Upload and shard a job across multiple accounts")
    p_dist.add_argument("file", help="Python script (.py) or notebook (.ipynb)")
    p_dist.add_argument("--title", "-t", required=True, help="Base title for shards")
    p_dist.add_argument("--accounts", help="Comma-separated account list")
    p_dist.add_argument("--total-items", type=int, default=10000000, help="Total items to shard")
    p_dist.add_argument("--start-offset", type=int, default=0, help="Start offset")
    p_dist.add_argument(
        "--accelerator",
        "-g",
        "--gpu",
        default="nvidia-tesla-t4-x2",
        choices=["nvidia-tesla-t4-x2", "nvidia-tesla-t4", "v3-8", "none"],
        help="Hardware accelerator",
    )
    p_dist.add_argument("--sessions-per-account", type=int, default=2, help="GPU sessions per account (max 2)")
    p_dist.add_argument("--no-internet", action="store_true", help="Disable internet access")
    p_dist.add_argument("--trial", action="store_true", help="Run 5-minute preflight trial")
    p_dist.add_argument("--timeout", type=int, help="Execution timeout in seconds")

    # logs
    p_logs = sub.add_parser("logs", help="Get logs for a run")
    p_logs.add_argument("run_id", help="Run ID")
    p_logs.add_argument("--remote", action="store_true", help="Fetch remote logs directly from Kaggle API")
    p_logs.add_argument("--tail", type=int, default=100, help="Lines to show (default: 100)")
    p_logs.add_argument("--full", action="store_true", help="Show full untruncated log")

    # stop
    p_stop = sub.add_parser("stop", help="Stop a run or distributed workload")
    p_stop.add_argument("target_id", help="Run ID or Workload ID")
    p_stop.add_argument("--workload", "-w", action="store_true", help="Target is a workload ID")

    args = parser.parse_args()

    commands = {
        "auth": cmd_auth,
        "status": cmd_status,
        "accounts": cmd_accounts,
        "runs": cmd_runs,
        "run": cmd_run,
        "distributed": cmd_distributed,
        "logs": cmd_logs,
        "stop": cmd_stop,
    }
    fn = commands.get(args.cmd)
    if not fn:
        parser.print_help()
        return 1
    return fn(args)


if __name__ == "__main__":
    sys.exit(main())
