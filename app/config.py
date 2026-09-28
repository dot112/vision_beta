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
    APP_VERSION: str = "1.0.0"
    APP_ENV: str = "development"
    DEBUG: bool = True
    SECRET_KEY: str = ""
    ADMIN_INITIAL_PASSWORD: str = ""

    HOST: str = "0.0.0.0"
    PORT: int = 8000
    WORKERS: int = 1

    DATABASE_URL: str = "sqlite+aiosqlite:///./data/factory_data.db"

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

    # ─── Health monitoring ───────────────────────────────────────────────────
    HEALTH_CHECK_INTERVAL_SECONDS: float = 2.0
    HEALTH_CAMERA_STALE_SECONDS: float = 5.0      # no new frame for this long -> camera stalled
    HEALTH_INFERENCE_STALE_SECONDS: float = 10.0  # frames arriving but none inferred -> inference stalled

    LOG_LEVEL: str = "INFO"
    LOG_DIR: str = "./logs"

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
