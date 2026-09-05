from fastapi import APIRouter
from services.ops_tracker import tracker

router = APIRouter(prefix="/api/ops", tags=["Operations"])


@router.get("/status")
async def ops_status():
    """Snapshot of long-running operations (stops, launches, refreshes).

    The UI polls this a few times a minute and keeps its buttons disabled +
    "Stopping..." / "Distributing..." while the matching server operation is
    still in flight - so a page refresh mid-operation doesn't let the user
    re-trigger Stop All or launch another distribute batch.
    """
    return {"success": True, **tracker.snapshot()}
