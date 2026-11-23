# syntax=docker/dockerfile:1

FROM python:3.12-slim-bookworm AS builder
COPY --from=ghcr.io/astral-sh/uv:0.13.8 /uv /uvx /bin/

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project --no-editable

COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# Hard gate: the WebUI ships inside the installed wheel. `-I` keeps PYTHONPATH,
# cwd and user site off sys.path, so this reads .venv's package, not /app/src.
RUN /app/.venv/bin/python -I - <<'PY'
import importlib.resources as res

names = ("index.html", "app.js", "styles.css")
static = res.files("codearts2api") / "static"
missing = [n for n in names if not (static / n).is_file() or not (static / n).read_bytes().strip()]
if missing:
    raise SystemExit("Installed WebUI assets missing or empty: " + ", ".join(missing))
print(f"WebUI assets verified: {static}")
PY

FROM python:3.12-slim-bookworm
WORKDIR /app

RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin app \
    && mkdir -p /app/auths /app/data \
    && chown -R app:app /app

# Runtime imports the wheel installed in .venv; source and build metadata are
# build-time only.
COPY --from=builder --chown=app:app /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CA2A_HOST=0.0.0.0 \
    CA2A_PORT=8787 \
    CA2A_AUTH_DIR=/app/auths \
    CA2A_DATA_DIR=/app/data

EXPOSE 8787

USER app

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/healthz', timeout=4)"

CMD ["codearts2api", "serve", "--host", "0.0.0.0", "--port", "8787"]
