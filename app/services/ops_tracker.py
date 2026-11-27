import threading
import time

_RUN_STOP = "run_stop:"
_WORKLOAD_STOP = "workload_stop:"
_EXT_STOP = "ext_stop:"
_DISTRIBUTE = "distribute"
_LAUNCH_RUN = "launch_run"
_REFRESH_QUOTAS = "refresh_quotas"


class OpsTracker:

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}
        self._started: dict[str, float] = {}

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

    def snapshot(self) -> dict[str, object]:
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


def run_stop_key(run_id: str) -> str:
    return _RUN_STOP + run_id


def workload_stop_key(workload_id: str) -> str:
    return _WORKLOAD_STOP + workload_id
