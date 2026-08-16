"""In-memory registry of long-running operations (stops, launches, refreshes).

The UI disables buttons while an operation is in flight and must keep them
disabled even if the page is refreshed mid-operation (e.g. refresh while
"Stop All" is still stopping shards, or while a distributed launch is still
pushing kernels). Every long-running route registers its operation here at
entry and clears it in a finally block; the frontend polls /api/ops/status
every few seconds and renders buttons from this state.

This is process-local state, exactly like the asyncio semaphores and log
subscriber registries elsewhere in the codebase: the app runs as a single
uvicorn worker, so a module-level store is correct. On server restart the
registry resets, which is fine - the operations themselves were interrupted
anyway.
"""

import threading
import time
from typing import Dict, List

_RUN_STOP = "run_stop:"          # + run_id -> a Kaggle run is being stopped
_WORKLOAD_STOP = "workload_stop:"  # + workload_id -> Stop All running on a workload
_EXT_STOP = "ext_stop:"            # + account/ref -> external kernel stop in flight
_DISTRIBUTE = "distribute"          # a distributed launch is pushing shards
_LAUNCH_RUN = "launch_run"          # a single-run push is in flight
_REFRESH_QUOTAS = "refresh_quotas"  # quota refresh (all or single) in flight


class OpsTracker:
    """Tracks active operations keyed by string; keys may be shared (counts)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # key -> number of concurrent owners (>=1 means active)
        self._counts: Dict[str, int] = {}
        self._started: Dict[str, float] = {}

    def begin(self, key: str) -> None:
        with self._lock:
            self._counts[key] = self._counts.get(key, 0) + 1
            self._started[key] = time.time()

    def end(self, key: str) -> None:
        with self._lock:
            count = self._counts.get(key, 0) - 1
            if count <= 0:
                self._counts.pop(key, None)
                self._started.pop(key, None)
            else:
                self._counts[key] = count

    def is_active(self, key: str) -> bool:
        with self._lock:
            return self._counts.get(key, 0) > 0

    def snapshot(self) -> Dict[str, object]:
        with self._lock:
            keys = sorted(self._counts.keys())
        return {
            "stopping_run_ids": [
                k[len(_RUN_STOP):] for k in keys if k.startswith(_RUN_STOP)
            ],
            "stopping_workload_ids": [
                k[len(_WORKLOAD_STOP):] for k in keys if k.startswith(_WORKLOAD_STOP)
            ],
            "stopping_kernel_refs": [
                k[len(_EXT_STOP):] for k in keys if k.startswith(_EXT_STOP)
            ],
            "distributing": _DISTRIBUTE in keys,
            "single_launching": _LAUNCH_RUN in keys,
            "refreshing_quotas": _REFRESH_QUOTAS in keys,
        }


tracker = OpsTracker()


# --- helpers so call sites read as intent, not string concatenation ---------

def run_stop_key(run_id: str) -> str:
    return _RUN_STOP + run_id


def workload_stop_key(workload_id: str) -> str:
    return _WORKLOAD_STOP + workload_id
