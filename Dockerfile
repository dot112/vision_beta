# syntax=docker/dockerfile:1
# Linux image for the vision server. Serves ONNX models on CPU; PyTorch and
# Ultralytics (model training/export, requirements-training.txt) are left out
# to keep the image small.

# ── Build stage: install the Python packages into a virtualenv ────────────────
FROM python:3.12-slim AS build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH

COPY requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

# ── Runtime stage ─────────────────────────────────────────────────────────────
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH \
    APP_ENV=production \
    DEBUG=false \
    HOST=0.0.0.0 \
    PORT=8000 \
    WORKERS=1 \
    INFERENCE_DEVICE=cpu \
    TMPDIR=/app/uploads/.tmp

# Unprivileged user; it owns only the directories the server writes to.
RUN groupadd --system --gid 10001 vision \
    && useradd --system --uid 10001 --gid vision --home-dir /app --shell /usr/sbin/nologin vision

COPY --from=build /opt/venv /opt/venv

WORKDIR /app
# Application code stays owned by root, so the server cannot modify it.
COPY . .
RUN python -m compileall -q /app/app /app/main.py /app/alembic /app/docker \
    && mkdir -p data logs model_store uploads/.tmp certs/mqtt \
    && chown -R vision:vision data logs model_store uploads certs

USER vision

# Database, settings, models, uploads, MQTT certificates and logs live here.
# Mount them as volumes (docker-compose.yml does) so they survive upgrades.
# Temporary files (large uploads in progress) go to uploads/.tmp (TMPDIR),
# which the supervisor empties at every start.
VOLUME ["/app/data", "/app/logs", "/app/model_store", "/app/uploads", "/app/certs"]

# 8000: HTTP API and dashboards. The UDP discovery beacon (8888) is only
# useful with the host network, where no port needs publishing.
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT', '8000'), timeout=4)"]

# SIGTERM lets the server finish open requests and put PLC outputs in their
# safe state; allow it time (docker-compose.yml sets stop_grace_period).
STOPSIGNAL SIGTERM

# SECRET_KEY (and ADMIN_INITIAL_PASSWORD on first start) must be supplied at
# run time, e.g. `docker compose` with a .env file or `docker run --env-file .env`.
# The supervisor runs uvicorn on 0.0.0.0 with one worker and exits when the
# server stops answering, so the restart policy starts a fresh container.
ENTRYPOINT ["python", "/app/docker/supervise.py"]
