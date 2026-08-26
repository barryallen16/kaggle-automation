"""Versioned Kaggle kernel-output fetcher (internal helper, run as a script).

Why this exists:
  `kaggle kernels output` downloads only the LATEST version's output, and
  CLI 2.x parses "<owner>/<slug>/<version>" without ever USING the version.
  But kagglesdk's ApiListKernelSessionOutputRequest has a `version_label`
  field the backend honours - so we talk to the SDK directly.

Kaggle publishes a per-version output snapshot when a version FINALIZES -
complete, error, OR cancelled. Fetching the exact cancelled version of a
stopped notebook therefore recovers its partial /kaggle/working contents,
which a naive latest-only pull misses (the latest becomes our stop-stub).

Usage (account credentials come from the environment, like every CLI call):
  python kaggle_versioned_output.py meta  OWNER SLUG
      -> prints {"current_version_number": N}
  python kaggle_versioned_output.py fetch OWNER SLUG VERSION OUT_DIR
      -> downloads that version's files + log into OUT_DIR,
         prints {"saved": [names]}
"""
import json
import os
import sys


def _api():
    from kaggle.api.kaggle_api_extended import KaggleApi
    api = KaggleApi()
    api.authenticate()
    return api


def current_version(owner: str, slug: str):
    """Returns the kernel's current (latest pushed) version number, or None."""
    api = _api()
    results = api.kernels_list(user=owner, search=slug, page_size=100) or []
    want_ref = f"{owner}/{slug}"
    for k in results:
        ref = getattr(k, "ref", "") or ""
        kslug = getattr(k, "slug", "") or ""
        if ref == want_ref or kslug == slug:
            v = getattr(k, "current_version_number", 0) or 0
            return int(v) if v else None
    return None


def fetch_version_output(owner: str, slug: str, version: int, out_dir: str):
    """Downloads ONE specific version's output files + log into out_dir."""
    from kagglesdk.kernels.types.kernels_api_service import (
        ApiListKernelSessionOutputRequest,
    )
    import requests

    api = _api()
    os.makedirs(out_dir, exist_ok=True)
    saved = []

    request = ApiListKernelSessionOutputRequest()
    request.user_name = owner
    request.kernel_slug = slug
    request.version_label = str(version)

    with api.build_kaggle_client() as kaggle:
        response = kaggle.kernels.kernels_api_client.list_kernel_session_output(request)

    for item in response.files or []:
        name = item.file_name
        dest = os.path.join(out_dir, name)
        parent = os.path.dirname(dest)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with requests.get(item.url, stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(256 * 1024):
                    f.write(chunk)
        saved.append(name)

    if response.log:
        log_path = os.path.join(out_dir, f"{slug}.log")
        with open(log_path, "w", encoding="utf-8", newline="") as f:
            f.write(response.log)
        saved.append(os.path.basename(log_path))

    return saved


def main(argv):
    if len(argv) >= 3 and argv[0] == "meta":
        owner, slug = argv[1], argv[2]
        print(json.dumps({"current_version_number": current_version(owner, slug)}))
        return 0

    if len(argv) >= 5 and argv[0] == "fetch":
        owner, slug, version, out_dir = argv[1], argv[2], int(argv[3]), argv[4]
        saved = fetch_version_output(owner, slug, version, out_dir)
        print(json.dumps({"saved": saved}))
        return 0

    sys.stderr.write(__doc__ or "usage: meta|fetch\n")
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as exc:  # surfaced through stderr; caller logs the tail
        sys.stderr.write(f"ERROR: {exc}\n")
        sys.exit(1)
