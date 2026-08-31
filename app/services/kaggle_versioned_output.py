"""Versioned Kaggle kernel-output fetcher (internal helper, run as a script).

Talks straight to Kaggle's kagglesdk HTTP routes (POST + camelCase JSON +
Bearer access_token) so we depend ONLY on httpx - not on the `kaggle`
python package being installed in this environment.

Routes replicated from kagglesdk 2.2.4 (kaggle_http_client.py
KaggleEnv.PROD + kagglesdk/kernels/services/kernels_api_service.py):
  POST https://api.kaggle.com/v1/kernels.KernelsApiService/ListKernels
       body {user, search, page, pageSize}
       -> {kernels: [{ref, currentVersionNumber, ...}]}
  POST https://api.kaggle.com/v1/kernels.KernelsApiService/ListKernelSessionOutput
       body {userName, kernelSlug, pageSize, versionLabel?, pageToken?}
       -> {files: [{url, fileName}], log, nextPageToken}

(The earlier draft hit www.kaggle.com/api/v1/... - Kaggle redirects
that to its marketing HTML site, which is why the helper returned a
"failed (rc=1)" with an HTML body. The actual API base is
api.kaggle.com and the path embeds the gRPC-style service+method name.)

Kaggle finalizes EVERY version (complete/error/cancelled) with its own
output snapshot; version_label selects which snapshot we get. A result
containing ONLY a log counts as FAILURE - that is the signature of the
stop-stub/latest-only trap this helper exists to escape.

Usage (account credentials come from the environment):
  python kaggle_versioned_output.py meta  OWNER SLUG
      -> {"current_version_number": N}
  python kaggle_versioned_output.py fetch OWNER SLUG VERSION OUT_DIR
      -> {"saved": [names], "tried": [labels], "notes": [...]}
  python kaggle_versioned_output.py list OWNER [SEARCH] [PAGE] [PAGESIZE]
      -> {"kernels": [{ref, title, slug, author, lastRunTime, currentVersionNumber, ...}]}

Exit codes: 0 ok (meta may report null), 3 nothing-recovered (fetch),
1 hard error, 2 usage.
"""
import json
import os
import sys

import httpx

# Production API base. The kagglesdk resolves this to
# https://api.kaggle.com in KaggleEnv.PROD (see kagglesdk/kaggle_env.py).
KAGGLE_BASE = os.getenv("KAGGLE_API_BASE", "https://api.kaggle.com")
KERNELS_SERVICE = "kernels.KernelsApiService"
TIMEOUT = float(os.getenv("VERSIONED_HTTP_TIMEOUT_SECONDS", "60"))


class KaggleError(Exception):
    pass


def _token() -> str:
    tok = (os.getenv("KAGGLE_API_TOKEN") or "").strip()
    if tok:
        return tok
    cfg_dir = os.getenv("KAGGLE_CONFIG_DIR") or ""
    for cand in (os.path.join(cfg_dir, "access_token"),
                 os.path.expanduser("~/.kaggle/access_token")):
        try:
            with open(cand, "r", encoding="utf-8") as f:
                tok = f.read().strip()
                if tok and not tok.startswith("{"):
                    return tok
        except OSError:
            continue
    raise KaggleError("no KAGGLE_API_TOKEN / access_token found in environment")


def _post(client: httpx.Client, method: str, payload: dict):
    """POST to https://api.kaggle.com/v1/<service>/<method> with Bearer auth."""
    url = f"{KAGGLE_BASE}/v1/{KERNELS_SERVICE}/{method}"
    r = client.post(url, json=payload)
    if r.status_code >= 400:
        # Truncate the body: a 200-OK HTML page is the signature of being
        # routed to the marketing site (wrong host or wrong path).
        snippet = r.text[:200].replace("\n", " ")
        raise KaggleError(f"{method} -> HTTP {r.status_code}: {snippet}")
    try:
        data = r.json()
    except Exception as e:
        snippet = r.text[:200].replace("\n", " ")
        raise KaggleError(f"{method} -> non-JSON response (HTML?): {snippet}")
    if isinstance(data, dict) and isinstance(data.get("code"), int) and data["code"] >= 400:
        raise KaggleError(f"{method} -> API error {data['code']}: {str(data.get('message'))[:200]}")
    return data


def _download_files(client: httpx.Client, files: list, out_dir: str) -> list:
    saved = []
    for item in files:
        url = item.get("url") or ""
        name = item.get("fileName") or item.get("name")
        if not url or not name:
            continue

        # Kaggle normally returns absolute URLs for the download endpoint, but
        # if it ever returns a relative one, prepend the prod base.
        if url.startswith("/"):
            url = f"{KAGGLE_BASE}{url}"

        dest = os.path.join(out_dir, name)
        parent = os.path.dirname(dest)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with client.stream("GET", url, timeout=TIMEOUT) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in resp.iter_bytes(256 * 1024):
                    f.write(chunk)
        saved.append(name)
    return saved


def _list_output_page(client: httpx.Client, owner: str, slug: str,
                      label: str = "", page_token: str = "", page_size: int = 200):
    payload = {"userName": owner, "kernelSlug": slug, "pageSize": page_size}
    if label:
        payload["versionLabel"] = label
    if page_token:
        payload["pageToken"] = page_token
    return _post(client, "ListKernelSessionOutput", payload)


def _list_files_page(client: httpx.Client, owner: str, slug: str,
                     label: str = "", page_token: str = "", page_size: int = 100):
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


def list_kernels(owner: str, search: str = "", page: int = 1, page_size: int = 20):
    """Lists kernels owned/visible to owner via ListKernels API."""
    payload: dict = {"user": owner, "page": page, "pageSize": page_size}
    if search:
        payload["search"] = search
    with httpx.Client(timeout=TIMEOUT,
                      headers={"Authorization": f"Bearer {_token()}"}) as client:
        data = _post(client, "ListKernels", payload)
        return data.get("kernels") or [], data.get("nextPageToken") or ""


def list_versions(owner: str, slug: str, max_versions: int = 20):
    """Lists per-version snapshots for a kernel, newest first.

    For each version `N` (current down to 1), probes:
      - ListKernelFiles with versionLabel (creationDate of first file -> version time)
      - GetKernelSessionStatus with versionLabel (running/complete/error/stopped)
      - ListKernelSessionOutput file count (how many output files that version published)
    Returns list of {version, label, creationTime, fileCount, status, hasOutput}.
    Times are ISO strings from the API when available, else "".
    """
    cur = current_version(owner, slug)
    # Fallback: if current_version couldn't be determined (private kernel, search miss, etc.),
    # probe descending from 20 to find the highest version that actually exists.
    # Try both status and files existence - some versions are log-only and status may still succeed.
    # Use all plausible label formats.
    probe_labels = lambda v: [str(v), f"{v}.0", f"v{v}", f"version-{v}", f"version{v}", f"Version{v}"]
    if not cur:
        try:
            with httpx.Client(timeout=TIMEOUT, headers={"Authorization": f"Bearer {_token()}"}) as client:
                for probe in range(20, 0, -1):
                    found = False
                    for label in probe_labels(probe):
                        try:
                            _get_status(client, owner, slug, label=label)
                            found = True
                            break
                        except KaggleError:
                            pass
                        try:
                            fdata = _list_files_page(client, owner, slug, label=label, page_size=1)
                            found = True
                            break
                        except KaggleError:
                            pass
                        try:
                            odata = _list_output_page(client, owner, slug, label=label, page_size=1)
                            found = True
                            break
                        except KaggleError:
                            pass
                    if found:
                        cur = probe
                        break
        except Exception:
            pass
        if not cur:
            try:
                with httpx.Client(timeout=TIMEOUT, headers={"Authorization": f"Bearer {_token()}"}) as client:
                    _get_kernel_metadata(client, owner, slug)
                    cur = 1
            except Exception:
                return []
    # Clamp to sane range
    try:
        max_versions = max(1, min(50, int(max_versions or 20)))
    except Exception:
        max_versions = 20
    start = cur
    end = max(1, cur - max_versions + 1)
    versions = []
    candidates_fmt = lambda v: [str(v), f"{v}.0", f"v{v}", f"version-{v}", f"version{v}", f"Version{v}"]
    with httpx.Client(timeout=TIMEOUT,
                      headers={"Authorization": f"Bearer {_token()}"}) as client:
        for v in range(start, end - 1, -1):
            entry = {"version": v, "label": str(v), "creationTime": "", "fileCount": 0, "status": "unknown", "hasOutput": False}
            # Try each label spelling for files
            for label in candidates_fmt(v):
                try:
                    fdata = _list_files_page(client, owner, slug, label=label, page_size=100)
                    files = fdata.get("files") or []
                    if files:
                        entry["fileCount"] = len(files)
                        # Use earliest file's creationDate as version time if available
                        # API returns creationDate string per file
                        times = [f.get("creationDate") or f.get("creation_date") or "" for f in files]
                        times = [t for t in times if t]
                        if times:
                            # Pick most recent file time
                            times_sorted = sorted(times)
                            entry["creationTime"] = times_sorted[-1]
                        entry["label"] = label
                        entry["hasOutput"] = True
                        break
                    # No files but call succeeded -> version exists but published nothing (log-only)
                    entry["label"] = label
                    entry["hasOutput"] = False
                    break
                except KaggleError:
                    continue
            # Status for this version
            for label in candidates_fmt(v):
                try:
                    sdata = _get_status(client, owner, slug, label=label)
                    st = sdata.get("status") or sdata.get("Status") or "unknown"
                    # Normalize enum names like "KernelWorkerStatus.COMPLETE" -> "complete"
                    st = str(st).split(".")[-1].lower()
                    if st:
                        entry["status"] = st
                    break
                except KaggleError:
                    continue
            # If still no time, try output log's file time via ListKernelSessionOutput
            if not entry["creationTime"]:
                for label in candidates_fmt(v):
                    try:
                        odata = _list_output_page(client, owner, slug, label=label, page_size=1)
                        # ListKernelSessionOutput doesn't have creation time, but we can use file-less hint
                        # If we get here without error, version exists
                        if odata.get("files") or odata.get("log"):
                            entry["label"] = label
                            break
                    except KaggleError:
                        continue
            versions.append(entry)
    return versions


def fetch_version_log(owner: str, slug: str, version: int) -> str:
    """Fetches log for a specific version snapshot."""
    candidates = [str(version), f"version-{version}", f"version{version}"]
    with httpx.Client(timeout=TIMEOUT,
                      headers={"Authorization": f"Bearer {_token()}"},
                      follow_redirects=True) as client:
        for label in candidates:
            try:
                data = _list_output_page(client, owner, slug, label=label, page_size=200)
                log = data.get("log") or ""
                if log:
                    return log
                # Even if log empty but call succeeded, version exists - return empty
                return ""
            except KaggleError:
                continue
    return ""


def _get_kernel_metadata(client: httpx.Client, owner: str, slug: str):
    """Fetches single kernel metadata via GetKernel (more reliable for currentVersionNumber)."""
    try:
        data = _post(client, "GetKernel", {"userName": owner, "kernelSlug": slug})
        # Response may be {metadata: {...}} or direct
        meta = data.get("metadata") or data
        if isinstance(meta, dict):
            v = meta.get("currentVersionNumber") or meta.get("current_version_number")
            if v:
                return int(v)
        # Some responses nest under "metadata" with camelCase
        if isinstance(data.get("metadata"), dict):
            v = data["metadata"].get("currentVersionNumber")
            if v:
                return int(v)
    except KaggleError:
        pass
    return None

def current_version(owner: str, slug: str):
    """Latest pushed version number of the kernel, or None if undeterminable.

    Search first; private kernels sometimes don't match search, so fall back
    to paging the user's full kernel list and matching by ref. Finally try
    GetKernel which is more reliable for currentVersionNumber.
    """
    want_ref = f"{owner}/{slug}"
    with httpx.Client(timeout=TIMEOUT,
                      headers={"Authorization": f"Bearer {_token()}"}) as client:
        try:
            data = _post(client, "ListKernels",
                         {"user": owner, "search": slug, "page": 1, "pageSize": 100})
            for k in data.get("kernels") or []:
                if (k.get("ref") or "") == want_ref or (k.get("slug") or "") == slug:
                    v = k.get("currentVersionNumber") or k.get("current_version_number") or 0
                    if v:
                        return int(v)
        except KaggleError:
            pass

        # Fallback: page through everything owned by this account
        try:
            for page in range(1, 6):
                data = _post(client, "ListKernels",
                             {"user": owner, "page": page, "pageSize": 100})
                kernels = data.get("kernels") or []
                for k in kernels:
                    if (k.get("ref") or "") == want_ref or (k.get("slug") or "") == slug:
                        v = k.get("currentVersionNumber") or k.get("current_version_number") or 0
                        if v:
                            return int(v)
                if len(kernels) < 100:
                    break
        except KaggleError:
            pass
        # Final fallback: GetKernel is more reliable for version number
        v = _get_kernel_metadata(client, owner, slug)
        if v:
            return v
    return None


def fetch_version_output(owner: str, slug: str, version: int, out_dir: str):
    """Downloads ONE specific version's output into out_dir.

    Tries every plausible versionLabel spelling; a page consisting solely of
    a log (the stop-stub signature) is rejected, not saved. Returns
    (saved_names, notes).
    """
    os.makedirs(out_dir, exist_ok=True)
    candidates = []
    for cand in (str(version), f"{version}.0", f"v{version}", f"version-{version}", f"version{version}", f"Version{version}"):
        if cand not in candidates:
            candidates.append(cand)

    notes, tried = [], []
    saved: list = []
    got_log = ""

    with httpx.Client(timeout=TIMEOUT,
                      headers={"Authorization": f"Bearer {_token()}"},
                      follow_redirects=True) as client:
        for label in candidates:
            try:
                page_token = ""
                page_files: list = []
                page_log = ""
                while True:
                    data = _list_output_page(client, owner, slug,
                                             label=label, page_token=page_token)
                    page_files.extend(data.get("files") or [])
                    if data.get("log"):
                        page_log = data["log"]
                    page_token = data.get("nextPageToken") or ""
                    if not page_token:
                        break

                tried.append(label)
                # Strip the log-only signature: real artifacts are required.
                if page_files:
                    saved.extend(_download_files(client, page_files, out_dir))
                    if page_log:
                        got_log = page_log
                    break  # this label worked - done
                notes.append(f"label '{label}': no output files published for "
                             f"this version (log-only or empty)")
            except KaggleError as e:
                tried.append(label)
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
        return 3  # nothing recoverable - caller decides fallback

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
        print(json.dumps({"versions": vers, "current_version": vers[0]["version"] if vers else None}))
        return 0

    if len(argv) >= 4 and argv[0] == "log":
        owner, slug, version = argv[1], argv[2], int(argv[3])
        log = fetch_version_log(owner, slug, version)
        print(json.dumps({"log": log}))
        return 0

    sys.stderr.write(__doc__ or "")
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        sys.exit(1)