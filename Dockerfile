FROM python:3.12-slim-bookworm

# Synology DS1817+ is x86_64; this image matches that arch.
WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY hearth ./hearth
COPY workspace ./workspace

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    WORKSPACE_PATH=/app/workspace \
    HEARTH_AUTH_DB=/app/data/hearth-auth.db \
    HEARTH_MEMORY_DB=/app/data/hearth-memory.db \
    HEARTH_PORT=8787

RUN mkdir -p /app/data

EXPOSE 8787

# /readyz is liveness plus the markers that say Hearth can actually serve a
# house turn (tool registry populated, databases open). It makes no network
# calls, so probing it every 30s costs nothing. Unconfigured integrations are
# reported as degraded rather than unhealthy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=5 \
    CMD curl -fsS http://127.0.0.1:8787/readyz || exit 1

CMD ["python", "-m", "hearth"]
