from config import BASE_DIR
from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/presets", tags=["Presets"])

PRESETS_DIR = BASE_DIR / "benchmarks"

# Fixed map (keys, not paths) so a crafted URL can never escape PRESETS_DIR.
PRESETS = {
    "serve-qwen38-plain": {
        "file": "serve_qwen3_8_plain.py",
        "title": "serve-qwen38-plain",
        "accelerator": "nvidia-tesla-t4-x2",
        "label": "Serve, stays up: Qwen3.8-27B (plain, T4 x2)",
        "recommended": True,
    },
    "serve-qwen38-draft": {
        "file": "serve_qwen3_8_draft.py",
        "title": "serve-qwen38-draft",
        "accelerator": "nvidia-tesla-t4-x2",
        "label": "Serve, stays up: Qwen3.8-27B (draft, T4 x2)",
        "recommended": False,
    },
    "bench-qwen38-plain": {
        "file": "bench_qwen38_plain.py",
        "title": "bench-qwen38-plain",
        "accelerator": "nvidia-tesla-t4-x2",
        "label": "Benchmark: Qwen3.8-27B serve (plain, T4 x2)",
        "recommended": False,
    },
    "bench-qwen38-draft": {
        "file": "bench_qwen38_draft.py",
        "title": "bench-qwen38-draft",
        "accelerator": "nvidia-tesla-t4-x2",
        "label": "Benchmark: Qwen3.8-27B serve (speculative draft, T4 x2)",
        "recommended": False,
    },
}


@router.get("")
async def list_presets():
    out = []
    for key, p in PRESETS.items():
        out.append(
            {
                "key": key,
                "label": p["label"],
                "title": p["title"],
                "accelerator": p["accelerator"],
                "filename": p["file"],
                "recommended": p["recommended"],
                "available": (PRESETS_DIR / p["file"]).is_file(),
            }
        )
    return {"success": True, "presets": out}


@router.get("/{key}")
async def get_preset(key: str):
    p = PRESETS.get(key)
    if not p:
        raise HTTPException(status_code=404, detail="Unknown preset")
    path = PRESETS_DIR / p["file"]
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Preset file missing")
    return {
        "success": True,
        "key": key,
        "title": p["title"],
        "accelerator": p["accelerator"],
        "filename": p["file"],
        "code": path.read_text(encoding="utf-8"),
    }
