import hashlib
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import create_async_engine

from test_attempt_flow import db, seed_participant
from app.config import Settings
from app.integrations.uoj.repository import UOJRepository
from app.security import hash_secret


@pytest.mark.asyncio
async def test_uoj_password_digest_and_banned_accounts():
    engine = create_async_engine("sqlite+aiosqlite://")
    digest = "a" * 32
    stored = hashlib.md5(("231250001" + digest).encode()).hexdigest()
    async with engine.begin() as connection:
        await connection.execute(text("CREATE TABLE user_info (username TEXT, password TEXT, usergroup TEXT)"))
        await connection.execute(text("INSERT INTO user_info VALUES (:u, :p, 'U')"), {"u":"231250001","p":stored})
    repository = object.__new__(UOJRepository)
    repository.engine = engine
    try:
        assert await repository.verify_user_password("231250001", digest.upper())
        assert not await repository.verify_user_password("231250001", "b" * 32)
        assert not await repository.verify_user_password("missing", digest)
        assert not await repository.verify_user_password("231250001", "invalid")
        async with engine.begin() as connection:
            await connection.execute(text("UPDATE user_info SET usergroup='B'"))
        assert not await repository.verify_user_password("231250001", digest)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_password_database_failure_safe_error_and_backup_login(db, caplog):
    from app.main import app
    from app.database import get_db
    import app.api.student as student
    quiz, participant = await seed_participant(db)
    quiz.quiz_code_hash = hash_secret("TEST1234")
    await db.commit()
    class Repository:
        async def verify_user_password(self, *args):
            raise OperationalError("sensitive_sql", {"secret":"do-not-log"}, Exception("denied"))
    async def test_db():
        yield db
    previous = getattr(app.state, "uoj_repository", None)
    app.state.uoj_repository = Repository()
    app.dependency_overrides[get_db] = test_db
    try:
        with patch.object(student, "get_settings", return_value=Settings(uoj_password_auth_enabled=True, uoj_password_client_salt="synthetic")):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                path = f"/api/quiz/{quiz.id}/login"
                response = await client.post(path, json={"student_number":participant.student_number,"uoj_password_hash":"a"*32})
                assert response.status_code == 503
                assert "备用测评码" in response.json()["detail"]
                assert "student_session" not in response.cookies
                assert "do-not-log" not in caplog.text + response.text
                assert "sensitive_sql" not in caplog.text + response.text
                backup = await client.post(path, json={"student_number":participant.student_number,"quiz_code":"TEST1234"})
                assert backup.status_code == 200
    finally:
        app.dependency_overrides.clear()
        app.state.uoj_repository = previous
