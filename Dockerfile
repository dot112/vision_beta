# Linux image for the vision server. Serves ONNX models on CPU; PyTorch and
# Ultralytics (model training/export) are left out to keep the image small.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    APP_ENV=production \
    DEBUG=false \
    INFERENCE_DEVICE=cpu

WORKDIR /app

COPY requirements-runtime.txt ./
RUN pip install -r requirements-runtime.txt

RUN useradd --system --create-home --uid 10001 vision
COPY --chown=vision:vision . .
RUN mkdir -p data logs model_store uploads \
    && chown -R vision:vision data logs model_store uploads

USER vision

# Database, saved settings, models and logs live here; mount them as volumes
# so they survive container upgrades.
VOLUME ["/app/data", "/app/logs", "/app/model_store", "/app/uploads"]

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/discovery', timeout=4)"

# SECRET_KEY (and ADMIN_INITIAL_PASSWORD on first start) must be supplied at
# run time, e.g. `docker run --env-file .env ...`. One worker only: runtime
# state such as camera connections is held in process memory.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
