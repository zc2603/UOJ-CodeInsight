from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    app_name: str = "UOJ AI 代码理解测评"
    environment: str = "development"
    database_url: str = "postgresql+asyncpg://uoj_quiz:uoj_quiz@127.0.0.1:5432/uoj_quiz"
    secret_key: str = Field(default="change-me-in-production", min_length=16)
    cookie_secure: bool = False
    session_hours: int = 12
    business_timezone: str = "Asia/Shanghai"

    student_username_regex: str = r"^[0-9]+$"
    source_code_max_chars: int = 50_000
    student_answer_max_chars: int = 5_000
    problem_statement_max_chars: int = 50_000
    archive_max_bytes: int = 2_000_000
    archive_uncompressed_max_bytes: int = 2_000_000

    uoj_database_url: str = ""
    uoj_http_base_url: str = "http://127.0.0.1:8080"
    uoj_judger_name: str = ""
    uoj_judger_password: str = ""
    uoj_password_auth_enabled: bool = False
    uoj_password_client_salt: str = ""

    llm_provider: str = "mock"
    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-v4-pro"
    llm_reasoning_effort: Literal["low", "high", "max"] = "max"
    llm_timeout_seconds: float = 180.0
    llm_max_tokens: int = 12_000
    # This limit is per Uvicorn worker. Four production workers make the
    # default effective process-wide ceiling approximately 20 model calls.
    llm_max_concurrency: int = Field(default=5, ge=1, le=200)
    grading_review_confidence_threshold: float = Field(default=0.75, ge=0, le=1)

    generation_global_concurrency: int = Field(default=20, ge=1, le=100)
    generation_workers: int = Field(default=5, ge=1, le=20)
    generation_poll_seconds: float = Field(default=3, gt=0)
    generation_task_timeout_seconds: float = Field(default=900, gt=0)
    generation_lease_seconds: float = Field(default=90, gt=0)
    generation_max_attempts: int = Field(default=3, ge=1, le=5)

    db_pool_size: int = 10
    db_max_overflow: int = 10
    attempt_maintenance_interval_seconds: float = Field(default=30, gt=0)
    # Includes semaphore queueing and all retries, not student answering time.
    attempt_preparing_timeout_seconds: float = Field(default=1800, gt=0)


@lru_cache
def get_settings() -> Settings:
    return Settings()
