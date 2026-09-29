"""Pydantic settings for every service. Reads the variables listed in ``.env.example``.

Secrets (database URL, AI keys) deliberately have **no defaults**: a service that is
started without them fails fast instead of silently using a baked-in credential.
AI keys may be set to an empty string (``GEMINI_API_KEY=``) to run in fallback mode.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Simulator -------------------------------------------------------------------
    SIMULATOR_URL: str = "http://simulator-api:8000"
    SIMULATION_SPEED: float = 8
    TICK_MINUTES: int = 15
    SIMULATOR_START_MODE: Literal["paused", "running"] = "paused"
    SIM_HTTP_TIMEOUT_S: float = 3.0

    # --- Infrastructure (secrets: no defaults) ---------------------------------------
    DATABASE_URL: str
    REDIS_URL: str = "redis://redis:6379/0"

    # --- AI (cloud only; secrets: no defaults, empty string = fallback mode) ----------
    TYPESAFE_API_KEY: str
    GEMINI_API_KEY: str
    GEMINI_MODEL: str = "gemini-flash-lite-latest"  # 2.0/2.5 models are retired (DECISIONS D29)
    JEV_MODEL: str = "jev-latest"
    LLM_PROVIDER: Literal["gemini", "groq"] = "gemini"  # System 2 provider for cognitive-svc
    GROQ_API_KEY: str = ""  # only needed when LLM_PROVIDER=groq; empty = template/fallback mode
    GROQ_MODEL: str = "openai/gpt-oss-120b"
    CHAOS_BAD_AI_KEYS: bool = False

    # --- Decision policy -------------------------------------------------------------
    DECISION_INTERVAL_TICKS: int = Field(default=4, ge=1)
    AUTO_APPROVE_NOUL_MIN: float = Field(default=0.85, ge=0, le=1)
    AUTO_APPROVE_URGENCY_MAX: float = 4.5
    STAGED_DECISION_TTL_TICKS: int = Field(default=16, ge=1)
    MIN_LOT_LITERS: float = Field(default=500, ge=0)
    ROUTE_CAP_MODE: Literal["per_route_per_cycle", "per_allocation"] = "per_route_per_cycle"
    DEPOT_CONSTRAINT_DERATE: float = Field(default=0.5, gt=0, le=1)
    JEV_TIMEOUT_MS: int = 600
    GEMINI_TIMEOUT_MS: int = 3000
    BREAKER_FAILURE_THRESHOLD: int = Field(default=5, ge=1)
    BREAKER_OPEN_SECONDS: float = Field(default=30, gt=0)

    # --- Tunables --------------------------------------------------------------------
    HORIZON_TICKS: int = 24
    COVER_TICKS: int = 32
    SYNC_MAX_HZ: float = 4
    SNAPSHOT_RETENTION_TICKS: int = 2000
    DEMO_CONTROLS: bool = False
    MODEL_DIR: str = "/models"  # forecast-svc model artifacts (mounted volume)

    # --- internal service URLs (compose DNS names) --------------------------------------------------------------
    FORECAST_URL: str = "http://forecast-svc:8102"
    DECISION_URL: str = "http://decision-svc:8103"
    COGNITIVE_URL: str = "http://cognitive-svc:8104"
    INGESTION_URL: str = "http://ingestion-svc:8101"
    AUTONOMOUS_DISPATCH: bool = True  # false = the loop still plans/stages but never auto-commits
    LOG_LEVEL: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # required fields come from the environment
