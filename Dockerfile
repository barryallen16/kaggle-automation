FROM python:3.12-slim-trixie AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app
COPY pyproject.toml uv.lock .
RUN uv sync --locked
COPY app /app

FROM python:3.12-slim-trixie 
WORKDIR /app
COPY --from=builder /app /app
EXPOSE 8000
CMD [".venv/bin/fastapi", "run", "main.py","--host", "0.0.0.0","--port","8000"]
