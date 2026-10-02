"""Opt-in: new localhost synthetic DB, migration 0009→0010 and late probe race."""
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
from app.services import runtime_settings
from app.services.runtime_settings import RuntimeOptions, RuntimeUpdate, save_runtime, runtime_response


@pytest.mark.asyncio
async def test_probe_migration_and_late_result_isolation(monkeypatch):
    url = os.environ.get("MODEL_PROBE_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Requires new localhost quiz_probe_synthetic_* database")
    parsed = make_url(url)
    assert parsed.host in {"localhost", "127.0.0.1"} and parsed.database.startswith("quiz_probe_synthetic_")
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM admin_users")) == 0
        assert await connection.scalar(text("SELECT count(*) FROM runtime_configuration_audits")) == 0
    env, cwd = {**os.environ, "DATABASE_URL": url}, Path(__file__).resolve().parents[1]
    subprocess.run([sys.executable, "-m", "alembic", "downgrade", "0009_runtime_and_appeals"], env=env, cwd=cwd, check=True)
    user_id = uuid.uuid4()
    async with engine.begin() as connection:
        await connection.execute(text("INSERT INTO admin_users (id,username,password_hash,role,is_active,created_at) "
            "VALUES (:id,'synthetic-probe-teacher','synthetic','teacher',true,now())"), {"id": user_id})
        await connection.execute(text("INSERT INTO runtime_configuration_audits (id,revision,actor,values_json,created_at) "
            "VALUES (:id,1,'synthetic-probe-teacher','{}',now())"), {"id": uuid.uuid4()})
        await connection.execute(text("UPDATE runtime_configuration SET revision=1 WHERE id=1"))
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], env=env, cwd=cwd, check=True)
    async with engine.connect() as connection:
        assert await connection.scalar(text("SELECT model_tests_json FROM runtime_configuration_audits WHERE revision=1")) is None
    actor = AdminPrincipal(user_id, "synthetic-probe-teacher")
    base = Settings(_env_file=None, llm_provider="mock")
    started, release, calls = asyncio.Event(), asyncio.Event(), []
    async def delayed(settings, pending):
        calls.append(pending["model"]); started.set(); await release.wait()
        return {**pending, "status": "passed", "message": "synthetic"}
    monkeypatch.setattr(runtime_settings, "probe", delayed)
    async def save(expected, options):
        async with factory() as db:
            return await save_runtime(db, base, actor, RuntimeUpdate(expected_revision=expected, settings=options))
    changed = RuntimeOptions(generation_service="openai")
    first = asyncio.create_task(save(1, changed))
    try:
        await asyncio.wait_for(started.wait(), 5)
        with pytest.raises(HTTPException) as exc:
            await save(1, changed)
        assert exc.value.status_code == 409
        latest = await save(2, changed.model_copy(update={"max_tokens": 8000}))
        assert latest["revision"] == 3 and latest["model_tests"] == []
        release.set()
        result = await first
        assert result["revision"] == 2 and calls == ["gpt-5.6-sol"]
        async with factory() as db:
            response = await runtime_response(db, base)
            assert response["revision"] == 3 and response["settings"].max_tokens == 8000
            stored = await db.scalar(text("SELECT model_tests_json FROM runtime_configuration_audits WHERE revision=2"))
            assert stored[0]["status"] == "passed"
    finally:
        release.set(); await asyncio.gather(first, return_exceptions=True); await engine.dispose()
