import asyncio
import logging
import os
import secrets
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from jinja2 import Environment, FileSystemLoader

from app.config import BASE_DIR, APP_AUTH_TOKEN
from app.database import init_db
from app.services.account_manager import AccountManager
from app.services.session_monitor import SessionMonitor
from app import auth

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

    if not APP_AUTH_TOKEN:
        logger.warning(
            "APP_AUTH_TOKEN is NOT set - the dashboard API is UNAUTHENTICATED. "
            "Set it in .env before exposing this service beyond localhost."
        )

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

# Enable CORS (same-origin UI only; the dashboard is served from this server,
# so cross-origin access is never needed. Web origins must match host:port.)
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_credentials=False,
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
    return template.render(auth_enabled=bool(APP_AUTH_TOKEN))

@app.get("/api/health")
async def health_check():
    from app.config import KAGGLE_CLI_PATH
    cli_available = os.path.exists(KAGGLE_CLI_PATH) or bool(shutil.which("kaggle"))
    return {
        "status": "ok",
        "service": "kaggle-nb-automation",
        "cli_available": cli_available,
        "auth_enabled": bool(APP_AUTH_TOKEN)
    }

# ------------------------------------------------------------------
# Authentication (shared secret -> signed HttpOnly cookie)
# Enabled only when APP_AUTH_TOKEN is set in .env / environment.
# ------------------------------------------------------------------
@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page(request: Request, error: str = ""):
    if not APP_AUTH_TOKEN:
        return RedirectResponse("/")
    # Already signed in? Never show the form again - otherwise the browser's
    # back button lands on the stale /login entry and demands re-auth.
    if auth.is_request_authenticated(request, APP_AUTH_TOKEN):
        next_url = request.query_params.get("next") or "/"
        # Only allow relative, same-site paths ("/x" ok, "//evil" or URLs not)
        if not next_url.startswith("/") or next_url.startswith("//"):
            next_url = "/"
        return RedirectResponse(next_url, status_code=303)
    template = jinja_env.get_template("login.html")
    return template.render(error=error)

@app.post("/login", include_in_schema=False)
async def login_submit(request: Request, access_token: str = Form(...)):
    if not APP_AUTH_TOKEN:
        return RedirectResponse("/")
    if not secrets.compare_digest(access_token.strip(), APP_AUTH_TOKEN):
        await asyncio.sleep(0.6)  # brute-force dampener
        return RedirectResponse("/login?error=Invalid+access+token", status_code=303)

    # Land the user on the page they originally wanted (?next=...), not always "/"
    next_url = request.query_params.get("next") or "/"
    if not next_url.startswith("/") or next_url.startswith("//"):
        next_url = "/"

    response = RedirectResponse(next_url, status_code=303)
    response.set_cookie(
        key=auth.SESSION_COOKIE_NAME,
        value=auth.create_session_cookie_value(APP_AUTH_TOKEN),
        max_age=auth.SESSION_TTL_SECONDS,
        httponly=True,
        samesite="strict",
        path="/",
    )
    return response

@app.post("/logout", include_in_schema=False)
async def logout():
    response = RedirectResponse("/login", status_code=303)
    response.set_cookie(**auth.clear_session_cookie())
    return response

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    response = await call_next(request)

    # Auth pages/API must never be served from browser cache - a cached login
    # page (or cached 303 to it) makes the back button feel like a forced
    # re-login. Static assets keep normal caching.
    if not request.url.path.startswith("/static"):
        response.headers.setdefault("Cache-Control", "no-store")

    if not APP_AUTH_TOKEN or auth.should_skip_path(request.url.path):
        return response

    # WebSockets bypass HTTP middleware; guarded separately in the endpoint.
    if request.url.path.startswith("/api/"):
        if not auth.is_request_authenticated(request, APP_AUTH_TOKEN):
            return JSONResponse(status_code=401, content={"detail": "Not authenticated"})
        return response

    if not auth.is_request_authenticated(request, APP_AUTH_TOKEN):
        login_url = f"/login?next={request.url.path}"
        return RedirectResponse(login_url, status_code=303)
    return response
