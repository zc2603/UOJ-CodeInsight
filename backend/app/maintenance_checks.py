from __future__ import annotations

import argparse
import asyncio
import hashlib
from pathlib import Path

import httpx
from sqlalchemy import text

from app.config import get_settings
from app.integrations.uoj.repository import UOJRepository
from app.services.llm_provider import GENERATOR_VERSION, GRADER_VERSION, LIGHTWEIGHT_GENERATOR_VERSION, LIGHTWEIGHT_GRADER_VERSION


PROMPTS = Path(__file__).resolve().parent / "prompts"


def configured(value: str) -> str:
    return "configured" if bool(value) else "missing"


def check_config() -> int:
    settings = get_settings()
    print("configuration_scope=deployment_defaults; runtime settings are reported by smoke")
    print(f"environment={settings.environment}")
    print(f"cookie_secure={str(settings.cookie_secure).lower()}")
    print(f"llm_provider={settings.llm_provider}")
    print(f"llm_model={settings.llm_model}")
    print(f"llm_reasoning_effort={settings.llm_reasoning_effort}")
    print(f"llm_timeout_seconds={settings.llm_timeout_seconds}")
    print(f"llm_max_tokens={settings.llm_max_tokens}")
    print(f"llm_max_concurrency={settings.llm_max_concurrency}")
    print(f"grading_review_confidence_threshold={settings.grading_review_confidence_threshold}")
    print(f"generation_global_concurrency={settings.generation_global_concurrency}")
    print(f"quality_audit_enabled={settings.quality_audit_enabled}")
    print(f"lightweight_creation_enabled={settings.lightweight_creation_enabled}")
    print(f"quality_audit_timeout_seconds={settings.quality_audit_timeout_seconds}")
    print(f"quality_audit_max_tokens={settings.quality_audit_max_tokens}")
    print(f"grading_workers={settings.grading_workers}")
    print(f"grading_poll_seconds={settings.grading_poll_seconds}")
    print(f"grading_task_timeout_seconds={settings.grading_task_timeout_seconds}")
    print(f"generation_workers={settings.generation_workers}")
    print(f"generation_task_timeout_seconds={settings.generation_task_timeout_seconds}")
    print(f"generation_lease_seconds={settings.generation_lease_seconds}")
    print(f"generation_max_attempts={settings.generation_max_attempts}")
    print(f"attempt_maintenance_interval_seconds={settings.attempt_maintenance_interval_seconds}")
    print(f"attempt_preparing_timeout_seconds={settings.attempt_preparing_timeout_seconds}")
    print(f"llm_api_key={configured(settings.llm_api_key)}")
    print(f"openai_api_key={configured(settings.openai_api_key)}")
    print(f"openai_base_url={settings.openai_base_url}")
    print(f"uoj_database={configured(settings.uoj_database_url)}")
    print(f"uoj_judger_credentials={configured(settings.uoj_judger_name)}/{configured(settings.uoj_judger_password)}")
    print(f"uoj_password_auth={str(settings.uoj_password_auth_enabled).lower()}")
    print(f"uoj_password_client_salt={configured(settings.uoj_password_client_salt)}")
    return 0


def smoke_check() -> int:
    required = (
        PROMPTS / f"{GENERATOR_VERSION}.txt",
        PROMPTS / f"{GRADER_VERSION}.txt",
        PROMPTS / f"{LIGHTWEIGHT_GENERATOR_VERSION}.txt",
        PROMPTS / f"{LIGHTWEIGHT_GRADER_VERSION}.txt",
    )
    missing = [item.name for item in required if not item.is_file()]
    if missing:
        print(f"smoke=failed missing={','.join(missing)}")
        return 1
    print(f"smoke=ok prompt_version={GENERATOR_VERSION} grader_prompt_version={GRADER_VERSION} lightweight_prompt_version={LIGHTWEIGHT_GENERATOR_VERSION} lightweight_grader_version={LIGHTWEIGHT_GRADER_VERSION}")
    for name in ("main.py", "services/attempt_maintenance.py", "services/quiz_service.py", "services/generation_service.py",
                 "services/llm_provider.py", "schemas/llm.py", f"prompts/{GRADER_VERSION}.txt"):
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
        await db.execute(text("SELECT review_required FROM attempts LIMIT 0"))
        await db.execute(text("SELECT review_required, review_reason, question_validity FROM answers LIMIT 0"))
        await db.execute(text("SELECT draft_text, draft_revision, grading_token, grading_lease_until FROM attempts LIMIT 0"))
        timeout_counts = (await db.execute(text("SELECT status, COUNT(*) FROM attempts WHERE timed_out = true OR status = 'EXPIRED' GROUP BY status"))).all()
        print("timeout_submissions=" + (",".join(f"{state}:{count}" for state, count in timeout_counts) or "empty"))
        await db.execute(text("SELECT quality_state, quality_result, quality_token FROM generation_jobs LIMIT 0"))
        await db.execute(text("SELECT assessment_version, published_at, publish_answers FROM quizzes LIMIT 0"))
        await db.execute(text("SELECT revision, answer_text FROM answer_drafts LIMIT 0"))
        await db.execute(text("SELECT question_score_version FROM appeals LIMIT 0"))
        from app.services.quality_audit import QualityResult
        from pydantic import ValidationError
        import json
        samples = (await db.execute(text("SELECT quality_raw FROM generation_jobs WHERE quality_state = 'failed' AND quality_raw IS NOT NULL ORDER BY quality_finished_at DESC LIMIT 10"))).scalars().all()
        for raw in samples:
            try:
                QualityResult.model_validate_json(raw)
            except ValidationError as exc:
                print("quality_validation=" + json.dumps([{"loc": e["loc"], "type": e["type"]} for e in exc.errors(include_input=False, include_context=False, include_url=False)]))
                print("quality_response_length=" + str(len(raw)))
        await db.execute(text("SELECT settings_json, settings_revision FROM admin_users LIMIT 0"))
        await db.execute(text("SELECT entry_minutes, reopen_minutes, grade_bands FROM quizzes LIMIT 0"))
        await db.execute(text("SELECT appeal_window_days, appeal_prompt FROM quizzes LIMIT 0"))
        await db.execute(text("SELECT revision, values_json FROM runtime_configuration WHERE id = 1"))
        await db.execute(text("SELECT revision, seen_at FROM runtime_workers LIMIT 0"))
        await db.execute(text("SELECT model_tests_json FROM runtime_configuration_audits LIMIT 0"))
        assert revision == "0010_model_switch_tests" and control == 1
        from app.services.runtime_settings import runtime_response
        runtime = await runtime_response(db, get_settings())
        print("runtime_settings=" + json.dumps({"revision": runtime["revision"],
            "settings": runtime["settings"].model_dump(), "runtime": runtime["runtime"]}))
        print(f"preparation_schema=ok revision={revision}")
        print("preparation_jobs=" + (",".join(f"{state}:{count}" for state, count in counts) or "empty"))
        from app.generation_diagnostics import latest_generation
        import json
        print("latest_generation=" + json.dumps(await latest_generation(db), ensure_ascii=False))


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
            models = response.json().get("data", [])
            if not any(item.get("id") == settings.llm_model for item in models):
                print(f"deepseek=failed reason=model_not_listed model={settings.llm_model}")
                return 1
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
            if settings.uoj_password_auth_enabled:
                # Validate actual authentication column grants without reading user records.
                await connection.execute(text("SELECT username, password, usergroup FROM user_info WHERE 1=0"))
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
