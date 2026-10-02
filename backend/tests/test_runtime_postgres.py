"""Opt-in synthetic localhost PostgreSQL migration, global CAS and worker refresh."""
import asyncio
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.api.dependencies import AdminPrincipal
from app.config import Settings
from app.services.runtime_manager import RuntimeManager
from app.services.runtime_settings import RuntimeOptions, RuntimeUpdate, save_runtime, runtime_response


@pytest.mark.asyncio
async def test_runtime_migration_cas_and_multiple_process_receipts():
    url = os.environ.get("RUNTIME_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Requires a new isolated quiz_runtime_synthetic_* PostgreSQL database")
    parsed = make_url(url)
    assert parsed.host in {"localhost", "127.0.0.1"} and parsed.database.startswith("quiz_runtime_synthetic_")
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM quizzes")) == 0
        assert await connection.scalar(text("SELECT count(*) FROM admin_users")) == 0
    env = {**os.environ, "DATABASE_URL": url}
    cwd = Path(__file__).resolve().parents[1]
    subprocess.run([sys.executable, "-m", "alembic", "downgrade", "0008_teacher_settings"], cwd=cwd, env=env, check=True)
    user_id, quiz_id = uuid.uuid4(), uuid.uuid4()
    async with engine.begin() as connection:
        await connection.execute(text("INSERT INTO admin_users (id,username,password_hash,role,is_active,created_at) "
            "VALUES (:id,'synthetic-runtime-teacher','synthetic','teacher',true,now())"), {"id": user_id})
        await connection.execute(text("INSERT INTO quizzes (id,name,uoj_contest_id,quiz_code_hash,start_time,end_time,"
            "duration_minutes,minutes_per_question,question_mode,submission_cutoff,status,pre_generate,show_score_after_finish,created_at) "
            "VALUES (:id,'synthetic-before-runtime',1,'synthetic',now(),now(),20,4,'lightweight_v1',now(),'draft',false,false,now())"), {"id": quiz_id})
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=cwd, env=env, check=True)
    async with engine.connect() as connection:
        row = (await connection.execute(text("SELECT duration_minutes, minutes_per_question, appeal_window_days, appeal_prompt "
            "FROM quizzes WHERE id=:id"), {"id": quiz_id})).one()
        assert tuple(row) == (20, 4, None, None)
    base = Settings(_env_file=None, llm_provider="mock")
    principal = AdminPrincipal(user_id, "synthetic-runtime-teacher")
    first, second = RuntimeManager(factory, base), RuntimeManager(factory, base)
    await asyncio.gather(first.start(), second.start())
    original = await first.snapshot("generation")
    async def save(model):
        async with factory() as db:
            try:
                return await save_runtime(db, base, principal, RuntimeUpdate(expected_revision=0,
                    settings=RuntimeOptions(generation_service="openai", openai_model=model)))
            except HTTPException as exc:
                return exc.status_code
    outcomes = await asyncio.gather(save("gpt-5.6-sol"), save("gpt-6-astra"))
    assert sorted(item if isinstance(item, int) else 200 for item in outcomes) == [200, 409]
    first.last_refresh = second.last_refresh = float("-inf")
    await asyncio.gather(first.refresh(), second.refresh())
    async with factory() as db:
        response = await runtime_response(db, base)
        assert response["revision"] == 1
        assert response["runtime"]["active_workers"] == response["runtime"]["loaded_workers"] == 2
    assert original.llm_model == "deepseek-flash"
    assert (await first.snapshot("generation")).llm_model == (await second.snapshot("generation")).llm_model
    await engine.dispose()
