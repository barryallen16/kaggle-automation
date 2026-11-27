import json
import os
import shutil
import sys
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from urllib.parse import urlsplit

import httpx

KAGGLE_BASE = os.getenv("KAGGLE_API_BASE", "https://api.kaggle.com")
USAGE = (
    "Usage:\n"
    "  python kaggle_versioned_output.py meta  OWNER SLUG\n"
    "  python kaggle_versioned_output.py fetch OWNER SLUG VERSION OUT_DIR\n"
    "  python kaggle_versioned_output.py list OWNER [SEARCH] [PAGE] [PAGESIZE]\n"
    "  python kaggle_versioned_output.py versions OWNER SLUG [MAX_VERSIONS]\n"
    "  python kaggle_versioned_output.py log OWNER SLUG VERSION\n"
)
KERNELS_SERVICE = "kernels.KernelsApiService"
TIMEOUT = float(os.getenv("VERSIONED_HTTP_TIMEOUT_SECONDS", "60"))


class KaggleError(Exception):
    pass


def _version_labels(v: int) -> list[str]:
    return [str(v), f"v{v}", f"{v}.0", f"version-{v}", f"version{v}", f"Version{v}"]


def _is_permission_error(exc: Exception) -> bool:
    msg = str(exc or "")
    if "403" not in msg:
        return False
    low = msg.lower()
    return "permission" in low or "kernels.get" in low


def _token(explicit: str | None = None) -> str:
    if explicit and explicit.strip():
        return explicit.strip()
    tok = (os.getenv("KAGGLE_API_TOKEN") or "").strip()
    if tok:
        return tok
    cfg_dir = os.getenv("KAGGLE_CONFIG_DIR") or ""
    for cand in (
        os.path.join(cfg_dir, "access_token"),
        os.path.expanduser("~/.kaggle/access_token"),
    ):
        try:
            with open(cand, "r", encoding="utf-8") as f:
                tok = f.read().strip()
                if tok and not tok.startswith("{"):
                    return tok
        except OSError:
            continue
    raise KaggleError("no KAGGLE_API_TOKEN / access_token found in environment")


def _post(client: httpx.Client, method: str, payload: dict):
    url = f"{KAGGLE_BASE}/v1/{KERNELS_SERVICE}/{method}"
    r = client.post(url, json=payload)
    if r.status_code >= 400:
        snippet = r.text[:200].replace("\n", " ")
        raise KaggleError(f"{method} -> HTTP {r.status_code}: {snippet}")
    try:
        data = r.json()
    except Exception:
        snippet = r.text[:200].replace("\n", " ")
        raise KaggleError(f"{method} -> non-JSON response (HTML?): {snippet}")
    if (
        isinstance(data, dict)
        and isinstance(data.get("code"), int)
        and data["code"] >= 400
    ):
        raise KaggleError(
            f"{method} -> API error {data['code']}: {str(data.get('message'))[:200]}"
        )
    return data


def _safe_dest(out_dir: str, rel_name: str) -> str | None:
    base = os.path.realpath(out_dir)
    dest = os.path.realpath(os.path.join(base, rel_name))
    try:
        if os.path.commonpath([base, dest]) != base or dest == base:
            return None
    except ValueError:
        return None
    return dest


def _is_same_origin(url: str) -> bool:
    base = urlsplit(KAGGLE_BASE)
    target = urlsplit(url)
    if not target.netloc:
        return True
    return (target.scheme, target.netloc) == (base.scheme, base.netloc)


def _stream_to_file(client: httpx.Client, url: str, dest: str) -> int:
    if _is_same_origin(url):
        with client.stream(
            "GET", url, timeout=TIMEOUT, follow_redirects=True
        ) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as f:
                f.writelines(resp.iter_bytes(256 * 1024))
            return resp.status_code
    extra = {"timeout": TIMEOUT, "follow_redirects": True}
    transport = getattr(client, "_transport", None)
    if (
        isinstance(httpx.Client, type)
        and transport is not None
        and not isinstance(transport, httpx.HTTPTransport)
    ):
        extra["transport"] = transport
    with (
        httpx.Client(**extra) as anon_client,
        anon_client.stream("GET", url) as resp,
    ):
        resp.raise_for_status()
        with open(dest, "wb") as f:
            f.writelines(resp.iter_bytes(256 * 1024))
        return resp.status_code


def _download_files(client: httpx.Client, files: list, out_dir: str) -> list:
    saved = []
    for item in files:
        url = item.get("url") or ""
        name = item.get("fileName") or item.get("name")
        if not url or not name:
            continue

        if url.startswith("/"):
            url = f"{KAGGLE_BASE}{url}"

        dest = _safe_dest(out_dir, name)
        if not dest:
            continue
        parent = os.path.dirname(dest)
        if parent:
            os.makedirs(parent, exist_ok=True)
        _stream_to_file(client, url, dest)
        saved.append(name)
    return saved


def _list_output_page(
    client: httpx.Client,
    owner: str,
    slug: str,
    label: str = "",
    page_token: str = "",
    page_size: int = 200,
):
    payload = {"userName": owner, "kernelSlug": slug, "pageSize": page_size}
    if label:
        payload["versionLabel"] = label
    if page_token:
        payload["pageToken"] = page_token
    return _post(client, "ListKernelSessionOutput", payload)


def _list_files_page(
    client: httpx.Client,
    owner: str,
    slug: str,
    label: str = "",
    page_token: str = "",
    page_size: int = 100,
):
    payload = {"userName": owner, "kernelSlug": slug, "pageSize": page_size}
    if label:
        payload["versionLabel"] = label
    if page_token:
        payload["pageToken"] = page_token
    return _post(client, "ListKernelFiles", payload)


def _get_status(client: httpx.Client, owner: str, slug: str, label: str = ""):
    payload = {"userName": owner, "kernelSlug": slug}
    if label:
        payload["versionLabel"] = label
    return _post(client, "GetKernelSessionStatus", payload)


def _get_kernel_metadata(client: httpx.Client, owner: str, slug: str, label: str = ""):
    payload = {"userName": owner, "kernelSlug": slug}
    if label:
        payload["versionLabel"] = label
    try:
        data = _post(client, "GetKernel", payload)
        meta = data.get("metadata") or data
        v = None
        t = ""
        if isinstance(meta, dict):
            v = meta.get("currentVersionNumber") or meta.get("current_version_number")
            t = meta.get("lastRunTime") or meta.get("last_run_time") or ""
        if v is None and isinstance(data.get("metadata"), dict):
            v = data["metadata"].get("currentVersionNumber") or data["metadata"].get(
                "current_version_number"
            )
            t = (
                data["metadata"].get("lastRunTime")
                or data["metadata"].get("last_run_time")
                or t
            )
        return (int(v) if v is not None else None), str(t or "")
    except KaggleError:
        return None, ""


def list_kernels(
    owner: str,
    search: str = "",
    page: int = 1,
    page_size: int = 20,
    token: str | None = None,
):
    payload: dict = {"user": owner, "page": page, "pageSize": page_size}
    if search:
        payload["search"] = search
    with httpx.Client(
        timeout=TIMEOUT, headers={"Authorization": f"Bearer {_token(token)}"}
    ) as client:
        data = _post(client, "ListKernels", payload)
        return data.get("kernels") or [], data.get("nextPageToken") or ""


def current_version(owner: str, slug: str, token: str | None = None) -> int | None:
    tok = _token(token)
    with httpx.Client(
        timeout=TIMEOUT, headers={"Authorization": f"Bearer {tok}"}
    ) as client:
        v, _ = _get_kernel_metadata(client, owner, slug)
        if v is not None and v > 0:
            return v

        want_ref = f"{owner}/{slug}"
        try:
            data = _post(
                client,
                "ListKernels",
                {"user": owner, "search": slug, "page": 1, "pageSize": 100},
            )
            for k in data.get("kernels") or []:
                if (k.get("ref") or "") == want_ref or (k.get("slug") or "") == slug:
                    v_val = (
                        k.get("currentVersionNumber")
                        or k.get("current_version_number")
                        or 0
                    )
                    if v_val:
                        return int(v_val)
        except KaggleError:
            pass

        try:
            for page in range(1, 6):
                data = _post(
                    client,
                    "ListKernels",
                    {"user": owner, "page": page, "pageSize": 100},
                )
                kernels = data.get("kernels") or []
                for k in kernels:
                    if (k.get("ref") or "") == want_ref or (
                        k.get("slug") or ""
                    ) == slug:
                        v_val = (
                            k.get("currentVersionNumber")
                            or k.get("current_version_number")
                            or 0
                        )
                        if v_val:
                            return int(v_val)
                if len(kernels) < 100:
                    break
        except KaggleError:
            pass

    return None


def _probe_single_version(owner: str, slug: str, v: int, token: str) -> dict[str, Any]:
    candidates = _version_labels(v)
    entry = {
        "version": v,
        "label": str(v),
        "creationTime": "",
        "fileCount": 0,
        "status": "unknown",
        "hasOutput": False,
    }

    with httpx.Client(
        timeout=TIMEOUT, headers={"Authorization": f"Bearer {token}"}
    ) as client:
        for label in candidates[:2]:
            try:
                _, v_time = _get_kernel_metadata(client, owner, slug, label=label)
                if v_time:
                    entry["creationTime"] = v_time
                    break
            except Exception:
                pass

        for label in candidates:
            try:
                sdata = _get_status(client, owner, slug, label=label)
                st = sdata.get("status") or sdata.get("Status") or "unknown"
                st = str(st).split(".")[-1].lower()
                if st:
                    entry["status"] = st
                    entry["label"] = label
                    break
            except KaggleError:
                continue

        for label in candidates:
            try:
                fdata = _list_files_page(
                    client, owner, slug, label=label, page_size=100
                )
                files = fdata.get("files") or []
                if files:
                    entry["fileCount"] = len(files)
                    entry["hasOutput"] = True
                    entry["label"] = label
                    if not entry["creationTime"]:
                        times = [
                            f.get("creationDate") or f.get("creation_date") or ""
                            for f in files
                        ]
                        times = [t for t in times if t]
                        if times:
                            entry["creationTime"] = max(times)
                    break
                entry["label"] = label
                break
            except KaggleError:
                continue

        if not entry["hasOutput"]:
            for label in candidates:
                try:
                    odata = _list_output_page(
                        client, owner, slug, label=label, page_size=1
                    )
                    if odata.get("files"):
                        entry["fileCount"] = len(odata.get("files"))
                        entry["hasOutput"] = True
                        entry["label"] = label
                        break
                    elif odata.get("log"):
                        entry["label"] = label
                        break
                except KaggleError:
                    continue

    return entry


def list_versions(
    owner: str, slug: str, max_versions: int = 20, token: str | None = None
) -> list[dict[str, Any]]:
    tok = _token(token)
    cur = current_version(owner, slug, token=tok)

    if not cur:
        def check_v(test_v: int) -> int | None:
            try:
                with httpx.Client(
                    timeout=TIMEOUT, headers={"Authorization": f"Bearer {tok}"}
                ) as c:
                    for lbl in (str(test_v), f"v{test_v}"):
                        try:
                            _get_status(c, owner, slug, label=lbl)
                            return test_v
                        except KaggleError:
                            pass
                        try:
                            fdata = _list_files_page(
                                c, owner, slug, label=lbl, page_size=1
                            )
                            if fdata is not None:
                                return test_v
                        except KaggleError:
                            pass
            except Exception:
                pass
            return None

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {executor.submit(check_v, p): p for p in range(50, 0, -1)}
            for fut in as_completed(futures):
                res = fut.result()
                if res and (cur is None or res > cur):
                    cur = res

    if not cur:
        return []

    try:
        max_versions = max(1, min(50, int(max_versions or 20)))
    except Exception:
        max_versions = 20

    start = cur
    end = max(1, cur - max_versions + 1)
    versions_to_probe = list(range(start, end - 1, -1))

    if not versions_to_probe:
        return []

    num_workers = min(4, max(1, len(versions_to_probe)))
    results: dict[int, dict[str, Any]] = {}

    with ThreadPoolExecutor(max_workers=num_workers) as executor:
        futures = {
            executor.submit(_probe_single_version, owner, slug, v, tok): v
            for v in versions_to_probe
        }
        for fut in as_completed(futures):
            v = futures[fut]
            try:
                results[v] = fut.result()
            except Exception:
                results[v] = {
                    "version": v,
                    "label": str(v),
                    "creationTime": "",
                    "fileCount": 0,
                    "status": "unknown",
                    "hasOutput": False,
                }

    return [results[v] for v in versions_to_probe if v in results]


def fetch_version_log(
    owner: str, slug: str, version: int, token: str | None = None
) -> str:
    candidates = _version_labels(version)
    tok = _token(token)
    with httpx.Client(
        timeout=TIMEOUT,
        headers={"Authorization": f"Bearer {tok}"},
        follow_redirects=True,
    ) as client:
        for label in candidates:
            try:
                data = _list_output_page(
                    client, owner, slug, label=label, page_size=200
                )
                log = data.get("log") or ""
                if log:
                    return log
                return ""
            except KaggleError:
                continue
    return ""


def _download_via_kernel_output_api(
    client: httpx.Client, owner: str, slug: str, version: int, out_dir: str
) -> list[str]:
    payload = {"ownerSlug": owner, "kernelSlug": slug, "versionNumber": int(version)}
    url = f"{KAGGLE_BASE}/v1/{KERNELS_SERVICE}/DownloadKernelOutput"
    tmp_zip: str | None = None
    try:
        redirect_url: str | None = None
        with client.stream("POST", url, json=payload, follow_redirects=False) as resp:
            if resp.status_code in (301, 302, 303, 307, 308):
                redirect_url = resp.headers.get("location")
            elif resp.status_code < 400:
                ct = (resp.headers.get("content-type") or "").lower()
                if "zip" in ct or "octet-stream" in ct:
                    with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as tf:
                        tmp_zip = tf.name
                        tf.writelines(resp.iter_bytes(256 * 1024))
                else:
                    body = resp.read()
                    try:
                        data = json.loads(body)
                        if isinstance(data, dict):
                            redirect_url = data.get("url")
                        elif isinstance(data, str) and data.startswith("http"):
                            redirect_url = data
                    except Exception:
                        redirect_url = None

        if redirect_url:
            if redirect_url.startswith("/"):
                redirect_url = f"{KAGGLE_BASE}{redirect_url}"
            with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as tf:
                tmp_zip = tf.name
            _stream_to_file(client, redirect_url, tmp_zip)

        if tmp_zip and os.path.isfile(tmp_zip) and zipfile.is_zipfile(tmp_zip):
            saved: list[str] = []
            with zipfile.ZipFile(tmp_zip) as zf:
                for member in zf.infolist():
                    if member.is_dir():
                        continue
                    dest = _safe_dest(out_dir, member.filename)
                    if not dest:
                        continue
                    parent = os.path.dirname(dest)
                    if parent:
                        os.makedirs(parent, exist_ok=True)
                    with zf.open(member, "r") as src, open(dest, "wb") as dst:
                        shutil.copyfileobj(src, dst, length=256 * 1024)
                    saved.append(member.filename)
            if saved:
                return saved
    except Exception:
        pass
    finally:
        if tmp_zip:
            try:
                os.unlink(tmp_zip)
            except OSError:
                pass
    return []


def fetch_version_output(
    owner: str, slug: str, version: int, out_dir: str, token: str | None = None
):
    os.makedirs(out_dir, exist_ok=True)
    candidates = _version_labels(version)

    notes, tried = [], []
    saved: list = []
    got_log = ""
    tok = _token(token)

    with httpx.Client(
        timeout=TIMEOUT,
        headers={"Authorization": f"Bearer {tok}"},
        follow_redirects=True,
    ) as client:
        direct_files = _download_via_kernel_output_api(
            client, owner, slug, version, out_dir
        )
        if direct_files:
            saved.extend(direct_files)

        if not saved:
            for label in candidates:
                try:
                    page_token = ""
                    page_files: list = []
                    page_log = ""
                    while True:
                        data = _list_output_page(
                            client, owner, slug, label=label, page_token=page_token
                        )
                        page_files.extend(data.get("files") or [])
                        if data.get("log"):
                            page_log = data["log"]
                        page_token = data.get("nextPageToken") or ""
                        if not page_token:
                            break

                    tried.append(label)
                    if page_files:
                        saved.extend(_download_files(client, page_files, out_dir))
                        if page_log:
                            got_log = page_log
                        break
                    notes.append(
                        f"label '{label}': no output files published for "
                        f"this version (log-only or empty)"
                    )
                except KaggleError as e:
                    tried.append(label)
                    if _is_permission_error(e):
                        notes.append(
                            f"label '{label}' failed: permission denied (kernels.get was denied for this token/kernel - skipping remaining labels, plain latest pull is the fallback): {e}"
                        )
                        break
                    notes.append(f"label '{label}' failed: {e}")

        if saved and got_log:
            log_path = os.path.join(out_dir, f"{slug}.log")
            with open(log_path, "w", encoding="utf-8", newline="") as f:
                f.write(got_log)
            saved.append(os.path.basename(log_path))

    return saved, notes


def main(argv):
    if len(argv) >= 3 and argv[0] == "meta":
        print(json.dumps({"current_version_number": current_version(argv[1], argv[2])}))
        return 0

    if len(argv) >= 5 and argv[0] == "fetch":
        owner, slug, version, out_dir = argv[1], argv[2], int(argv[3]), argv[4]
        saved, notes = fetch_version_output(owner, slug, version, out_dir)
        print(json.dumps({"saved": saved, "tried": notes}))
        if saved:
            return 0
        return 3

    if len(argv) >= 1 and argv[0] == "list":
        if len(argv) < 2:
            sys.stderr.write("list requires OWNER\n")
            return 2
        owner = argv[1]
        search = argv[2] if len(argv) > 2 else ""
        try:
            page = int(argv[3]) if len(argv) > 3 else 1
        except ValueError:
            page = 1
        try:
            page_size = int(argv[4]) if len(argv) > 4 else 20
        except ValueError:
            page_size = 20
        kernels, next_token = list_kernels(owner, search, page, page_size)
        print(json.dumps({"kernels": kernels, "nextPageToken": next_token}))
        return 0

    if len(argv) >= 1 and argv[0] == "versions":
        if len(argv) < 3:
            sys.stderr.write("versions requires OWNER SLUG\n")
            return 2
        owner, slug = argv[1], argv[2]
        try:
            max_v = int(argv[3]) if len(argv) > 3 else 20
        except ValueError:
            max_v = 20
        vers = list_versions(owner, slug, max_v)
        cur_v = vers[0]["version"] if vers else None
        print(json.dumps({"versions": vers, "current_version": cur_v}))
        return 0

    if len(argv) >= 4 and argv[0] == "log":
        owner, slug, version = argv[1], argv[2], int(argv[3])
        log = fetch_version_log(owner, slug, version)
        print(json.dumps({"log": log}))
        return 0

    sys.stderr.write(USAGE)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        sys.exit(1)
