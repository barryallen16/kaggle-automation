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

    if token:
        req_headers["Authorization"] = f"Bearer {token}"
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


def download_to_file(
    url: str,
    endpoint: str,
    dest_path: Path,
    token: str = "",
    timeout: int = 300,
) -> dict[str, Any]:
    full_url = f"{url.rstrip('/')}/{endpoint.lstrip('/')}"
    req_headers = {"User-Agent": f"kaggle-runner/{VERSION}"}
    if token:
        req_headers["Authorization"] = f"Bearer {token}"
        cookie = make_session_cookie(token)
        if cookie:
            req_headers["Cookie"] = f"{SESSION_COOKIE_NAME}={cookie}"

    req = urllib.request.Request(full_url, headers=req_headers, method="GET")
    tmp_path = dest_path.with_name(f".{dest_path.name}.part.{os.getpid()}")
    try:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        total = 0
        with urllib.request.urlopen(req, timeout=timeout) as resp, open(tmp_path, "wb") as out:
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                total += len(chunk)
        os.replace(tmp_path, dest_path)
        return {"success": True, "path": str(dest_path), "bytes": total}
    except urllib.error.HTTPError as e:
        tmp_path.unlink(missing_ok=True)
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
        return {"error": f"HTTP {e.code}: {detail}", "http_status": e.code}
    except urllib.error.URLError as e:
        tmp_path.unlink(missing_ok=True)
        return {"error": f"Cannot reach server at {url}: {e.reason}"}
    except Exception as e:
        tmp_path.unlink(missing_ok=True)
        return {"error": str(e)}


def _count_busy_gpu(acc: dict[str, Any]) -> int:
    if "active_gpu_runs" in acc:
        return int(acc["active_gpu_runs"] or 0)
    active = acc.get("active_runs") or []
    return sum(
        1
        for r in active
        if str((r or {}).get("accelerator") or "none").lower()
        not in ("none", "default", "cpu", "")
    )


def _gpu_hours_left(acc: dict[str, Any]) -> Any:
    if "remaining_gpu_hours" in acc:
        return acc["remaining_gpu_hours"]
    gpu = (acc.get("last_quota") or {}).get("gpu")
    if isinstance(gpu, dict) and gpu.get("limit") is not None:
        try:
            return round(
                max(0.0, float(gpu["limit"]) - float(gpu.get("used") or 0.0)), 1
            )
        except (TypeError, ValueError):
            pass
    return "N/A"


def _aggregate_accounts(accounts_data: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    summary = []
    total_free = 0
    for acc in accounts_data:
        u = acc.get("username", "unknown")
        busy = _count_busy_gpu(acc)
        gpu_left = _gpu_hours_left(acc)
        free = max(0, 2 - busy)
        total_free += free
        summary.append({
            "username": u,
            "free_slots": free,
            "busy_sessions": busy,
            "quota_hours": gpu_left,
        })
    return summary, total_free


def _extract_api_error(resp: dict[str, Any], fallback: str) -> str:
    msg = resp.get("error") or resp.get("detail") or resp.get("message")
    if msg:
        return str(msg)
    for item in resp.get("shards") or resp.get("failed") or ():
        if isinstance(item, dict):
            pr = item.get("push_result") if isinstance(item.get("push_result"), dict) else item
            err = pr.get("error") or pr.get("message") or pr.get("detail")
            if err:
                return f"{resp.get('status') or fallback}: {err}"
    return str(resp.get("status") or fallback)


def _check_api_error(resp: Any, fallback: str = "Request failed", require_dict: bool = False) -> None:
    if require_dict and not isinstance(resp, dict):
        fail(f"Unexpected non-JSON response: {str(resp)[:120]}", hint="Verify --url points to the kabila API server")
    if isinstance(resp, dict) and (resp.get("error") or resp.get("success") is False):
        fail(_extract_api_error(resp, fallback), hint=resp.get("help"))


def cmd_home(url: str, token: str) -> int:
    health = api_request(url, "/api/health", token=token)
    _check_api_error(health, require_dict=True)

    accounts_resp = api_request(url, "/api/accounts", token=token)
    _check_api_error(accounts_resp, require_dict=True)
    active_resp = api_request(url, "/api/runs/active", token=token)
    _check_api_error(active_resp, require_dict=True)

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

    if gitignore_path.exists():
        gi_text = gitignore_path.read_text(encoding="utf-8")
        if not re.search(r"^\.env(\b|\.|\*)", gi_text, re.MULTILINE):
            with open(gitignore_path, "a", encoding="utf-8") as f:
                f.write("\n# Secrets\n.env\n.env.*\n")
    else:
        gitignore_path.write_text("# Secrets\n.env\n.env.*\n", encoding="utf-8")

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

    health = api_request(url, "/api/health", token=token)
    _check_api_error(health)

    accounts_resp = api_request(url, "/api/accounts", token=token)
    _check_api_error(accounts_resp)
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
    _check_api_error(health, require_dict=True)

    accounts_resp = api_request(url, "/api/accounts", token=token)
    _check_api_error(accounts_resp, require_dict=True)
    active_resp = api_request(url, "/api/runs/active", token=token)
    _check_api_error(active_resp, require_dict=True)

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
    _check_api_error(resp)

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
    _check_api_error(resp)

    runs_data = resp.get("active_runs" if args.active else "runs", []) if isinstance(resp, dict) else []
    if not runs_data:
        label = "active" if args.active else "recent"
        emit({f"{label}_runs": f"0 {label} runs found"}, hints=["Run `kaggle_runner run <file>` to launch a kernel"])
        return 0

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
        _check_api_error(acc_resp)
        if isinstance(acc_resp, dict) and acc_resp.get("accounts"):
            for a in acc_resp["accounts"]:
                if _count_busy_gpu(a) < 2:
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

    _check_api_error(resp)

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
        _check_api_error(acc_resp)
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

    _check_api_error(resp)

    w_id = resp.get("workload_id") if isinstance(resp, dict) else None
    hints = [
        f"Run `kaggle_runner stop {w_id} --workload` to abort all shards" if w_id else "",
        "Run `kaggle_runner runs --active` to view active shard progress",
    ]
    emit(resp, hints=hints)
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    url, token = load_config(args.url, args.token)
    run_id = urllib.parse.quote(args.run_id, safe="")
    endpoint = f"/api/runs/{run_id}/logs?fetch_remote={'true' if args.remote else 'false'}"
    resp = api_request(url, endpoint, token=token)

    _check_api_error(resp)

    text = str(resp)
    lines = text.splitlines()
    total_lines = len(lines)

    if not args.full and total_lines > args.tail:
        shown_lines = lines[-args.tail:]
        print("\n".join(shown_lines))
        print(f"\n... (truncated, {total_lines} lines total)")
        print_hints([f"Run `kaggle_runner logs {args.run_id} --full` to see complete output"])
    else:
        print(text if text.strip() else "(no log output yet)")

    return 0


def cmd_outputs(args: argparse.Namespace) -> int:
    if args.zip and args.download:
        fail(
            "--zip and --download cannot be used together",
            hint="Use either --zip for all outputs or --download <file> for one file",
        )
    url, token = load_config(args.url, args.token)
    run_id = urllib.parse.quote(args.run_id, safe="")

    if args.pull:
        pull_resp = api_request(
            url,
            f"/api/runs/{run_id}/files/pull",
            token=token,
            method="POST",
            timeout=600,
        )
        _check_api_error(pull_resp)
        if not (args.download or args.zip):
            emit(
                pull_resp,
                hints=[
                    f"Run `kaggle_runner outputs {args.run_id} --zip` to download all files as ZIP",
                    f"Run `kaggle_runner outputs {args.run_id}` to list local and remote files",
                ],
            )
            return 0

    if args.zip:
        dest = Path(args.out).resolve() if args.out else Path.cwd() / f"{args.run_id}_outputs.zip"
        dl_resp = download_to_file(
            url, f"/api/runs/{run_id}/files/download-zip", dest, token=token, timeout=600
        )
        if dl_resp.get("error"):
            fail(dl_resp["error"], hint=dl_resp.get("help") or "Try `kaggle_runner outputs <run_id> --pull` first")
        emit({"run_id": args.run_id, "downloaded_zip": dl_resp["path"], "bytes": dl_resp["bytes"]})
        return 0

    if args.download:
        rel_name = args.download.lstrip("/")
        safe_leaf = Path(rel_name).name or "output.bin"
        dest = Path(args.out).resolve() if args.out else Path.cwd() / safe_leaf
        if dest.is_dir():
            dest = dest / safe_leaf
        encoded_file = urllib.parse.quote(rel_name, safe="/")
        dl_resp = download_to_file(
            url,
            f"/api/runs/{run_id}/files/download/{encoded_file}",
            dest,
            token=token,
            timeout=600,
        )
        if dl_resp.get("error"):
            fail(dl_resp["error"], hint=dl_resp.get("help") or f"Run `kaggle_runner outputs {args.run_id}` to inspect exact filenames")
        emit({"run_id": args.run_id, "downloaded_file": dl_resp["path"], "bytes": dl_resp["bytes"]})
        return 0

    files_resp = api_request(url, f"/api/runs/{run_id}/files", token=token)
    _check_api_error(files_resp)

    remote = files_resp.get("remote_files", []) if isinstance(files_resp, dict) else []
    local = files_resp.get("local_files", []) if isinstance(files_resp, dict) else []
    emit(
        {
            "run_id": args.run_id,
            "has_local_download": bool(local),
            "remote_files": remote,
            "local_files": local,
        },
        hints=[
            f"Run `kaggle_runner outputs {args.run_id} --pull` to sync outputs to the server",
            f"Run `kaggle_runner outputs {args.run_id} --zip` to download all outputs locally",
        ],
    )
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    url, token = load_config(args.url, args.token)
    target_id = urllib.parse.quote(args.target_id, safe="")
    if args.workload:
        endpoint = f"/api/distributed/{target_id}/stop"
    else:
        endpoint = f"/api/runs/{target_id}/stop"

    resp = api_request(url, endpoint, token=token, method="POST", timeout=240)
    _check_api_error(resp)

    emit(resp, hints=["Run `kaggle_runner runs --active` to verify session release"])
    return 0


def main() -> int:
    url, token = load_config()

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

    p_auth = sub.add_parser("auth", help="Save token in .env and ensure .env is gitignored")
    p_auth.add_argument("--token", "-t", required=True, help="APP_AUTH_TOKEN secret")
    p_auth.add_argument("--url", "-u", default=DEFAULT_URL, help=f"Server URL (default: {DEFAULT_URL})")

    sub.add_parser("status", help="Check server health, accounts, free GPU slots, and active runs")

    sub.add_parser("accounts", help="List registered Kaggle accounts and quota info")

    p_runs = sub.add_parser("runs", help="List runs")
    p_runs.add_argument("--active", action="store_true", help="List active runs only")
    p_runs.add_argument("--limit", type=int, default=20, help="Max runs to display")
    p_runs.add_argument("--fields", help="Comma-separated field list (e.g. id,title,status,account)")

    p_run = sub.add_parser("run", help="Upload and launch a single kernel run")
    p_run.add_argument("file", help="Python script (.py) or notebook (.ipynb)")
    p_run.add_argument("--title", "-t", help="Kernel title (max 50 chars)")
    p_run.add_argument("--account", "-a", help="Kaggle account username")
    p_run.add_argument(
        "--accelerator",
        "-g",
        "--gpu",
        default="nvidia-tesla-t4-x2",
        help="Accelerator (e.g. nvidia-tesla-t4-x2, nvidia-l4, nvidia-a100, tpu-v5e-8, none)",
    )
    p_run.add_argument("--no-internet", action="store_true", help="Disable internet access")
    p_run.add_argument("--trial", action="store_true", help="Run 5-minute preflight trial")
    p_run.add_argument("--timeout", type=int, help="Execution timeout in seconds")

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
        help="Accelerator (e.g. nvidia-tesla-t4-x2, nvidia-l4, nvidia-a100, tpu-v5e-8, none)",
    )
    p_dist.add_argument("--sessions-per-account", type=int, default=2, help="GPU sessions per account (max 2)")
    p_dist.add_argument("--no-internet", action="store_true", help="Disable internet access")
    p_dist.add_argument("--trial", action="store_true", help="Run 5-minute preflight trial")
    p_dist.add_argument("--timeout", type=int, help="Execution timeout in seconds")

    p_logs = sub.add_parser("logs", help="Get logs for a run")
    p_logs.add_argument("run_id", help="Run ID")
    p_logs.add_argument("--remote", action="store_true", help="Fetch remote logs directly from Kaggle API")
    p_logs.add_argument("--tail", type=int, default=100, help="Lines to show (default: 100)")
    p_logs.add_argument("--full", action="store_true", help="Show full untruncated log")

    p_out = sub.add_parser("outputs", help="List, pull, or download output files for a run")
    p_out.add_argument("run_id", help="Run ID")
    p_out.add_argument("--pull", action="store_true", help="Pull outputs from Kaggle onto the server")
    p_out.add_argument("--download", help="Download a single output filename locally")
    p_out.add_argument("--zip", action="store_true", help="Download all run outputs locally as a ZIP")
    p_out.add_argument("--out", help="Local destination file or directory path")

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
        "outputs": cmd_outputs,
        "stop": cmd_stop,
    }
    fn = commands.get(args.cmd)
    if not fn:
        parser.print_help()
        return 1
    return fn(args)


if __name__ == "__main__":
    sys.exit(main())
