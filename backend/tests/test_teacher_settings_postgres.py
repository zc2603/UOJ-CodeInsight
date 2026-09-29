"""Opt-in isolated PostgreSQL migration/CAS test; synthetic localhost DB only."""
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

from app.services.teacher_settings import TeacherSettings, SettingsUpdate, save_settings, load_settings


@pytest.mark.asyncio
async def test_postgres_migration_and_two_session_settings_cas():
    url = os.environ.get("SETTINGS_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set SETTINGS_TEST_DATABASE_URL to an empty isolated synthetic PostgreSQL database")
    parsed = make_url(url)
    assert parsed.host in {"127.0.0.1", "localhost"}
    assert parsed.database.startswith("quiz_settings_synthetic_")
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        assert (await connection.scalar(text("SELECT count(*) FROM admin_users"))) == 0
        assert (await connection.scalar(text("SELECT count(*) FROM quizzes"))) == 0
    env = {**os.environ, "DATABASE_URL": url}
    cwd = Path(__file__).resolve().parents[1]
    # Reconstruct the prior schema in this newly created, empty synthetic DB.
    subprocess.run([sys.executable, "-m", "alembic", "downgrade", "0007_lightweight_v1"], cwd=cwd, env=env, check=True)
    user_id, quiz_id = uuid.uuid4(), uuid.uuid4()
    async with engine.begin() as connection:
        await connection.execute(text("INSERT INTO admin_users (id,username,password_hash,role,is_active,created_at) "
            "VALUES (:id,'synthetic-teacher','synthetic','teacher',true,now())"), {"id": user_id})
        await connection.execute(text("INSERT INTO quizzes (id,name,uoj_contest_id,quiz_code_hash,start_time,end_time,"
            "duration_minutes,minutes_per_question,question_mode,submission_cutoff,status,pre_generate,show_score_after_finish,created_at) "
            "VALUES (:id,'synthetic-old',7,'synthetic',now(),now(),25,5,'lightweight_v1',now(),'draft',false,false,now())"), {"id": quiz_id})
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=cwd, env=env, check=True)
    async with engine.connect() as connection:
        row = (await connection.execute(text("SELECT duration_minutes, minutes_per_question, entry_minutes, reopen_minutes, grade_bands "
            "FROM quizzes WHERE id=:id"), {"id": quiz_id})).one()
        assert tuple(row) == (25, 5, 30, 30, None)
    async def save(minutes):
        async with factory() as db:
            try:
                return await save_settings(db, user_id, SettingsUpdate(expected_revision=0,
                    settings=TeacherSettings(entry_minutes=minutes)))
            except HTTPException as exc:
                return exc.status_code
    responses = await asyncio.gather(save(15), save(45))
    assert sorted(r if isinstance(r, int) else 200 for r in responses) == [200, 409]
    async with factory() as db:
        settings, revision = await load_settings(db, user_id)
        assert settings.entry_minutes in (15, 45) and revision == 1
    await engine.dispose()
