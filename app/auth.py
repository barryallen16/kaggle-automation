"""Shared-secret authentication: APP_AUTH_TOKEN -> HMAC-signed HttpOnly cookie.

Design:
- .env APP_AUTH_TOKEN acts as both the login credential and (hashed) the signing key,
  so rotating the token instantly invalidates all issued sessions.
- Cookie value: "<expiry_epoch>.<hmac_sha256(expiry)>" - constant-time verified.
- SameSite=Strict doubles as CSRF protection (cross-site requests carry no cookie).
- If APP_AUTH_TOKEN is unset, auth is DISABLED (local dev convenience); main.py
  logs a loud warning at startup.
"""
import hashlib
import hmac
import time

from fastapi import Request

SESSION_COOKIE_NAME = "kaggle_auto_session"
SESSION_TTL_SECONDS = 7 * 24 * 3600  # 7 days

EXEMPT_PATHS = {"/login", "/api/health"}
EXEMPT_PREFIXES = ("/static/",)

def _signing_key(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()

def _sign(key: bytes, expiry: str) -> str:
    return hmac.new(key, expiry.encode("utf-8"), hashlib.sha256).hexdigest()

def create_session_cookie_value(token: str, now: float | None = None) -> str:
    expiry = str(int((now or time.time()) + SESSION_TTL_SECONDS))
    return f"{expiry}.{_sign(_signing_key(token), expiry)}"

def verify_session_cookie(cookie_value: str | None, token: str) -> bool:
    if not token or not cookie_value or "." not in cookie_value:
        return False
    expiry_str, _, signature = cookie_value.partition(".")
    try:
        expiry = int(expiry_str)
    except ValueError:
        return False
    if expiry < time.time():
        return False
    expected = _sign(_signing_key(token), expiry_str)
    return hmac.compare_digest(signature, expected)

def is_request_authenticated(request: Request, token: str) -> bool:
    return verify_session_cookie(request.cookies.get(SESSION_COOKIE_NAME), token)

def should_skip_path(path: str) -> bool:
    return path in EXEMPT_PATHS or path.startswith(EXEMPT_PREFIXES)

def clear_session_cookie() -> dict:
    return {
        "key": SESSION_COOKIE_NAME,
        "value": "",
        "max_age": 0,
        "httponly": True,
        "samesite": "strict",
        "path": "/",
    }
