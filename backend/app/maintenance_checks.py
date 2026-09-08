from __future__ import annotations

import argparse
import asyncio
import hashlib
from pathlib import Path

import httpx
from sqlalchemy import text

from app.config import get_settings
from app.integrations.uoj.repository import UOJRepository


PROMPTS = Path(__file__).resolve().parent / "prompts"


def configured(value: str) -> str:
    return "configured" if bool(value) else "missing"


def check_config() -> int:
    settings = get_settings()
    print(f"environment={settings.environment}")
    print(f"cookie_secure={str(settings.cookie_secure).lower()}")
    print(f"llm_provider={settings.llm_provider}")
    print(f"llm_model={settings.llm_model}")
    print(f"llm_reasoning_effort={settings.llm_reasoning_effort}")
    print(f"generation_global_concurrency={settings.generation_global_concurrency}")
    print(f"generation_workers={settings.generation_workers}")
    print(f"generation_task_timeout_seconds={settings.generation_task_timeout_seconds}")
    print(f"generation_lease_seconds={settings.generation_lease_seconds}")
    print(f"generation_max_attempts={settings.generation_max_attempts}")
    print(f"attempt_maintenance_interval_seconds={settings.attempt_maintenance_interval_seconds}")
    print(f"attempt_preparing_timeout_seconds={settings.attempt_preparing_timeout_seconds}")
    print(f"llm_api_key={configured(settings.llm_api_key)}")
    print(f"uoj_database={configured(settings.uoj_database_url)}")
    print(f"uoj_judger_credentials={configured(settings.uoj_judger_name)}/{configured(settings.uoj_judger_password)}")
    print(f"uoj_password_auth={str(settings.uoj_password_auth_enabled).lower()}")
    print(f"uoj_password_client_salt={configured(settings.uoj_password_client_salt)}")
    return 0


def smoke_check() -> int:
    required = (
        PROMPTS / "question_generator_v4.txt",
        PROMPTS / "grader_v5.txt",
    )
    missing = [item.name for item in required if not item.is_file()]
    if missing:
        print(f"smoke=failed missing={','.join(missing)}")
        return 1
    print("smoke=ok prompt_version=v4 grader_prompt_version=v5")
    for name in ("main.py", "services/attempt_maintenance.py", "services/quiz_service.py", "services/generation_service.py"):
        path = PROMPTS.parent / name
        print(f"source={name} sha256={hashlib.sha256(path.read_bytes()).hexdigest()}")
    try:
        asyncio.run(check_preparation_schema())
    except Exception as exc:
        print(f"preparation_schema=failed reason={type(exc).__name__}")
        return 1
    return 0


async def check_preparation_schema():
    from app.database import SessionLocal
    async with SessionLocal() as db:
        revision = await db.scalar(text("SELECT version_num FROM alembic_version"))
        control = await db.scalar(text("SELECT id FROM generation_control WHERE id = 1"))
        await db.execute(text("SELECT pre_generate FROM quizzes LIMIT 0"))
        counts = (await db.execute(text("SELECT state, COUNT(*) FROM generation_jobs GROUP BY state"))).all()
        assert revision == "0003_pre_generation" and control == 1
        print(f"preparation_schema=ok revision={revision}")
        print("preparation_jobs=" + (",".join(f"{state}:{count}" for state, count in counts) or "empty"))


async def check_deepseek() -> int:
    settings = get_settings()
    if not settings.llm_api_key:
        print("deepseek=failed reason=api_key_missing")
        return 1
    try:
        async with httpx.AsyncClient(
            base_url=settings.llm_base_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {settings.llm_api_key}"},
            timeout=min(settings.llm_timeout_seconds, 30),
        ) as client:
            response = await client.get("models")
        if response.status_code == 200:
            print(f"deepseek=ok model={settings.llm_model}")
            return 0
        print(f"deepseek=failed http_status={response.status_code}")
        return 1
    except httpx.TimeoutException:
        print("deepseek=failed reason=timeout")
        return 1
    except httpx.TransportError as exc:
        print(f"deepseek=failed reason={type(exc).__name__}")
        return 1


async def check_uoj() -> int:
    settings = get_settings()
    if not settings.uoj_database_url:
        print("uoj=failed reason=database_not_configured")
        return 1
    repository = UOJRepository(settings.uoj_database_url, settings.business_timezone)
    try:
        async with repository.engine.connect() as connection:
            result = await connection.scalar(text("SELECT 1"))
        if result != 1:
            print("uoj=failed reason=unexpected_query_result")
            return 1
        print("uoj=ok access=read_only_session")
        return 0
    except Exception as exc:
        # Never render DB exceptions here: driver messages can contain connection URLs.
        print(f"uoj=failed reason={type(exc).__name__}")
        return 1
    finally:
        await repository.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Redacted UOJ Quiz maintenance checks")
    parser.add_argument("check", choices=("config", "smoke", "deepseek", "uoj"))
    args = parser.parse_args()
    if args.check == "config":
        return check_config()
    if args.check == "smoke":
        return smoke_check()
    if args.check == "deepseek":
        return asyncio.run(check_deepseek())
    return asyncio.run(check_uoj())


if __name__ == "__main__":
    raise SystemExit(main())
