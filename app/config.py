from __future__ import annotations

from functools import lru_cache
from typing import List
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    APP_NAME: str = "Industrial Vision API"
    APP_VERSION: str = "2.0.0"
    APP_ENV: str = "development"
    DEBUG: bool = True
    SECRET_KEY: str = ""
    ADMIN_INITIAL_PASSWORD: str = ""

    HOST: str = "0.0.0.0"
    PORT: int = 8000
    WORKERS: int = 1

    DATABASE_URL: str = "sqlite+aiosqlite:///./data/factory_data.db"
    # SQLite only. WAL lets the dashboards read while events are written;
    # NORMAL keeps every committed row through a crash of the server and loses
    # at most the last commits (never the file) on a power cut. Use FULL for
    # no loss on power cuts, or DELETE to keep the old rollback journal.
    SQLITE_JOURNAL_MODE: str = "WAL"
    SQLITE_SYNCHRONOUS: str = "NORMAL"
    SQLITE_BUSY_TIMEOUT_MS: int = 5000

    JWT_ALGORITHM: str = "HS256"

    # ── Session & Authentication ──────────────────────────────────────────────
    # Login session lifetime (Change this number to easily adjust session duration):
    SESSION_DURATION_DAYS: int = 2
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 2  # Exactly 2 Days (2880 mins)
    REFRESH_TOKEN_EXPIRE_DAYS: int = 2

    CORS_ORIGINS: str = "http://localhost:3000,http://localhost:8080"

    @property
    def cors_origin_list(self) -> List[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    # ── MQTT ──────────────────────────────────────────────────────────────────
    MQTT_BROKER_HOST: str = ""
    MQTT_BROKER_PORT: int = 8883
    MQTT_USERNAME: str = ""
    MQTT_PASSWORD: str = ""
    MQTT_TLS_ENABLED: bool = True
    MQTT_CA_CERT_PATH: str = ""

    # ── Modbus ────────────────────────────────────────────────────────────────
    MODBUS_HOST: str = "192.168.1.100"
    MODBUS_PORT: int = 502
    MODBUS_TIMEOUT: int = 3

    # ── Model / Inference ─────────────────────────────────────────────────────
    MODEL_STORE_PATH: str = "./model_store"
    MAX_MODEL_UPLOAD_BYTES: int = 536870912
    MAX_IMAGE_UPLOAD_BYTES: int = 10485760
    INFERENCE_DEVICE: str = "auto"   # "auto" = GPU if available (DirectML/CUDA), else CPU
    INFERENCE_THREADS: int = 0       # ONNX Runtime CPU threads; 0 = auto (2 with a GPU, ~1 per core on CPU)
    INFERENCE_CONFIDENCE: float = 0.5
    INFERENCE_NMS_THRESHOLD: float = 0.45
    # Detect API calls use the model one at a time. While cameras are connected
    # they get at most this share of the model's time, so live counting keeps
    # the rest; 1 = no limit. Requests past the queue limit get HTTP 429.
    API_INFERENCE_MAX_SHARE: float = 0.25
    API_INFERENCE_QUEUE_LIMIT: int = 8  # 0 = no limit
    # Requests per second each client address may send to /api/ on average,
    # with bursts up to RATE_LIMIT_BURST; past that it gets HTTP 429. 0 = off.
    RATE_LIMIT_PER_SECOND: float = 50.0
    RATE_LIMIT_BURST: int = 200

    # Cameras connected at the same time, across all production lines.
    MAX_CONNECTED_CAMERAS: int = 8

    # ─── Health monitoring ───────────────────────────────────────────────────
    HEALTH_CHECK_INTERVAL_SECONDS: float = 2.0
    HEALTH_CAMERA_STALE_SECONDS: float = 5.0      # no new frame for this long -> camera stalled
    HEALTH_INFERENCE_STALE_SECONDS: float = 10.0  # frames arriving but none inferred -> inference stalled

    # ─── Startup and reconnects ──────────────────────────────────────────────
    # Startup waits this long for saved cameras, the MQTT broker and PLC checks,
    # then starts serving while the rest keep connecting in the background, so
    # a camera that is still booting cannot hold the API down.
    STARTUP_CONNECT_WAIT_SECONDS: float = 15.0
    # A camera that could not be opened at startup (or when its line started)
    # is retried in the background, waiting longer after each failure up to this.
    CAMERA_RECONNECT_MAX_SECONDS: float = 60.0
    # Limits for opening and reading a network or USB camera stream.
    CAMERA_OPEN_TIMEOUT_SECONDS: float = 10.0
    CAMERA_READ_TIMEOUT_SECONDS: float = 5.0

    LOG_LEVEL: str = "INFO"
    LOG_DIR: str = "./logs"
    # logs/app.log, rotated at LOG_FILE_MAX_BYTES and keeping LOG_FILE_BACKUP_COUNT
    # old files. Everything also goes to stdout (docker logs).
    LOG_TO_FILE: bool = True
    LOG_FILE_MAX_BYTES: int = 10 * 1024 * 1024
    LOG_FILE_BACKUP_COUNT: int = 5

    @field_validator("SQLITE_JOURNAL_MODE")
    @classmethod
    def validate_sqlite_journal_mode(cls, value: str) -> str:
        value = value.strip().upper()
        if value not in {"WAL", "DELETE", "TRUNCATE", "PERSIST"}:
            raise ValueError("SQLITE_JOURNAL_MODE must be WAL, DELETE, TRUNCATE or PERSIST")
        return value

    @field_validator("SQLITE_SYNCHRONOUS")
    @classmethod
    def validate_sqlite_synchronous(cls, value: str) -> str:
        value = value.strip().upper()
        if value not in {"NORMAL", "FULL", "EXTRA"}:
            raise ValueError("SQLITE_SYNCHRONOUS must be NORMAL, FULL or EXTRA")
        return value

    @field_validator("SECRET_KEY")
    @classmethod
    def validate_secret_key(cls, value: str) -> str:
        if len(value) < 32 or value.lower() in {"change-me-in-production", "secret", "changeme"}:
            raise ValueError("SECRET_KEY must be set to a unique value of at least 32 characters")
        return value

    @field_validator("JWT_ALGORITHM")
    @classmethod
    def validate_jwt_algorithm(cls, value: str) -> str:
        if value not in {"HS256", "HS384", "HS512"}:
            raise ValueError("JWT_ALGORITHM must be a supported HMAC algorithm")
        return value

    @field_validator("ADMIN_INITIAL_PASSWORD")
    @classmethod
    def validate_initial_admin_password(cls, value: str) -> str:
        if value and len(value) < 20:
            raise ValueError("ADMIN_INITIAL_PASSWORD must be at least 20 characters")
        return value

    @field_validator("WORKERS")
    @classmethod
    def validate_worker_count(cls, value: int) -> int:
        if value != 1:
            raise ValueError("WORKERS must remain 1 while runtime state is process-local")
        return value

    @field_validator("API_INFERENCE_MAX_SHARE")
    @classmethod
    def validate_api_inference_share(cls, value: float) -> float:
        if not 0.0 < value <= 1.0:
            raise ValueError("API_INFERENCE_MAX_SHARE must be greater than 0 and at most 1")
        return value

    @field_validator("API_INFERENCE_QUEUE_LIMIT")
    @classmethod
    def validate_api_inference_queue_limit(cls, value: int) -> int:
        if value < 0:
            raise ValueError("API_INFERENCE_QUEUE_LIMIT must be 0 (no limit) or more")
        return value

    @field_validator("RATE_LIMIT_PER_SECOND")
    @classmethod
    def validate_rate_limit(cls, value: float) -> float:
        if value < 0:
            raise ValueError("RATE_LIMIT_PER_SECOND must be 0 (off) or more")
        return value

    @field_validator("RATE_LIMIT_BURST")
    @classmethod
    def validate_rate_limit_burst(cls, value: int) -> int:
        if value < 1:
            raise ValueError("RATE_LIMIT_BURST must be at least 1")
        return value

    @field_validator("MAX_MODEL_UPLOAD_BYTES", "MAX_IMAGE_UPLOAD_BYTES")
    @classmethod
    def validate_upload_limit(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("Upload size limits must be positive")
        return value


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
