from fastapi import APIRouter
from services.ops_tracker import tracker

router = APIRouter(prefix="/api/ops", tags=["Operations"])


@router.get("/status")
async def ops_status():
    return {"success": True, **tracker.snapshot()}
