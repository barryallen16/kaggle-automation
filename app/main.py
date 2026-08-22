import logging
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from jinja2 import Environment, FileSystemLoader

from app.config import BASE_DIR
from app.database import init_db
from app.services.account_manager import AccountManager
from app.services.session_monitor import SessionMonitor

# Import routers
from app.routers import accounts, runs, distributed, logs, files, settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("app.main")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Initializing Kaggle Automation Backend...")
    init_db()
    
    # Auto-initialize accounts from .env KAGGLE_APIKEYS on startup
    try:
        await AccountManager.initialize_from_env()
    except Exception as e:
        logger.warning(f"Account auto-initialization error: {e}")

    # Start background 12-hour session monitor
    await SessionMonitor.start()
    
    yield
    
    # Shutdown
    logger.info("Shutting down Kaggle Automation Backend...")
    await SessionMonitor.stop()

app = FastAPI(
    title="Kaggle Multi-Account Automation Platform",
    description="Centralized dashboard and API for managing multiple Kaggle accounts, streaming outputs, trial runs, and distributed workloads.",
    version="1.0.0",
    lifespan=lifespan
)

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers
app.include_router(accounts.router)
app.include_router(runs.router)
app.include_router(distributed.router)
app.include_router(logs.router)
app.include_router(files.router)
app.include_router(settings.router)

# Static files & Jinja templates
static_dir = BASE_DIR / "app" / "static"
static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

templates_dir = BASE_DIR / "app" / "templates"
templates_dir.mkdir(parents=True, exist_ok=True)
jinja_env = Environment(loader=FileSystemLoader(str(templates_dir)))

@app.get("/", response_class=HTMLResponse)
async def serve_index(request: Request):
    template = jinja_env.get_template("index.html")
    return template.render()

@app.get("/api/health")
async def health_check():
    return {"status": "ok", "service": "kaggle-nb-automation"}
