import uuid
from datetime import datetime, timedelta, timezone
import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_pre_generation import db, prepare_seed, complete_one, NoGeneration
from app.models import GenerationJob
from app.services import quality_audit as audit
from app.services import generation_service as generation
from app.services.quiz_service import start_attempt
from app.services.generation_service import regenerate_prepared
from app.config import Settings
from app.main import app
from app.database import get_db
from app.security import create_token
from app.services.llm_provider import MockLLMProvider, OpenAICompatibleLLMProvider


def outcome(verdict="pass", confidence=.99):
    return dict(questions=[dict(index=i, verdict=verdict, confidence=confidence, reason="具体证据") for i in (1,2)])


@pytest.mark.parametrize("verdict,confidence,attention", [("pass",.75,False),("pass",.74,True),("fail",.99,True),("uncertain",.99,True)])
def test_attention_is_not_a_score(verdict, confidence, attention):
    job = GenerationJob(id=uuid.uuid4(), quality_state="done", quality_result=outcome(verdict, confidence))
    assert audit.presentation(job,1,.75)["attention"] == attention
    job.quality_acknowledged = {"1":"viewed"}
    assert not audit.presentation(job,1,.75)["attention"]
    assert audit.presentation(job,2,.75)["attention"] == attention
    job.quality_state = "failed"
    assert audit.presentation(job,2,.75)["attention"]


def test_protocol_rejects_duplicate_missing_and_invalid_confidence():
    for payload in [dict(questions=outcome()["questions"][:1]), dict(questions=[outcome()["questions"][0]]*2),outcome(confidence=1.1)]:
        with pytest.raises(ValidationError):
            audit.QualityResult.model_validate(payload)


@pytest.mark.asyncio
async def test_queue_recovery_fences_late_results_and_never_changes_questions(db):
    quiz, participant = await prepare_seed(db)
    await complete_one(db)
    job = await db.scalar(select(GenerationJob).where(GenerationJob.state=="succeeded"))
    saved = job.result_json
    identity = await audit.claim(db)
    assert identity[0] == job.id
    assert await audit.claim(db) is None
    replacement = await audit.claim(db, datetime.now(timezone.utc)+timedelta(seconds=100))
    assert replacement[1] != identity[1]
    assert not await audit.finish(db,identity,"mock",result=outcome("fail"))
    assert await audit.finish(db,replacement,"mock",result=outcome())
    await db.refresh(job)
    assert job.result_json == saved and job.state == "succeeded"
    assert job.quality_state == "done"


@pytest.mark.asyncio
async def test_repeated_interruption_is_bounded_and_disabled_generation_not_enqueued(db):
    await prepare_seed(db)
    await complete_one(db, Settings(quality_audit_enabled=False))
    assert await audit.claim(db) is None
    await complete_one(db)
    await audit.claim(db)
    await audit.claim(db, datetime.now(timezone.utc)+timedelta(seconds=100))
    assert await audit.claim(db, datetime.now(timezone.utc)+timedelta(seconds=200)) is None
    failed = await db.scalar(select(GenerationJob).where(GenerationJob.quality_state=="failed"))
    assert failed.quality_attempts == 2


@pytest.mark.asyncio
async def test_teacher_only_audit_and_attention_do_not_block_open_or_student(db):
    quiz, participant = await prepare_seed(db)
    await complete_one(db)
    await complete_one(db)
    identity = await audit.claim(db)
    await audit.finish(db,identity,"mock",result=outcome("fail"),raw="private raw")
    async def test_db(): yield db
    app.dependency_overrides[get_db] = test_db
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
            path=f"/api/admin/quality-audits/{identity[0]}/1"
            assert (await client.get(path)).status_code==401
            assert (await client.post(path+"/acknowledge")).status_code==401
            client.cookies.set("admin_session",create_token(str(uuid.uuid4()),"admin",username="teacher"))
            rows_path=f"/api/admin/quizzes/{quiz.id}/results"
            assert (await client.get(rows_path)).json()[0]["quality_attention"]
            detail=(await client.get(path)).json()
            assert detail["verdict"]=="fail" and "raw" not in detail
            await generation.open_quiz(db,quiz.id)
            first=await start_attempt(db,Settings(),NoGeneration(),quiz_id=quiz.id,student_number=participant.student_number,session_id="a")
            assert "quality" not in first.model_dump()
            attempt=(await client.get(f"/api/admin/attempts/{first.attempt_id}")).json()
            assert any(q["quality"]["verdict"]=="fail" for q in attempt["questions"])
            for index in (1,2):
                assert (await client.post(f"/api/admin/quality-audits/{identity[0]}/{index}/acknowledge")).status_code==200
            assert not (await client.get(rows_path)).json()[0]["quality_attention"]
            assert (await client.post(f"/api/admin/quality-audits/{identity[0]}/request")).status_code==409
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_new_round_does_not_inherit_old_attention(db):
    quiz, participant = await prepare_seed(db)
    await complete_one(db)
    await complete_one(db)
    identity=await audit.claim(db)
    await audit.finish(db,identity,"mock",result=outcome("fail"))
    await regenerate_prepared(db,quiz.id,participant.student_number,1)
    from app.api.admin import _result_rows
    assert not (await _result_rows(db,quiz.id))[0].quality_attention


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["success","invalid","timeout","empty","length","fail"])
async def test_real_request_shape_offline_and_failures_preserve_generation(db,mode):
    import asyncio
    await prepare_seed(db)
    await complete_one(db)
    identity=await audit.claim(db)
    observed=[]
    async def handler(request):
        import json
        observed.append(json.loads(request.content))
        if mode=="timeout": await asyncio.sleep(2)
        content = audit.QualityResult.model_validate(outcome("fail" if mode=="fail" else "pass")).model_dump_json() if mode in ("success", "fail") else "bad json"
        if mode in ("empty", "length"): content = ""
        return httpx.Response(200,json={"choices":[{"finish_reason": "length" if mode=="length" else "stop", "message":{"content":content}}]})
    settings=Settings(llm_provider="openai-compatible",llm_api_key="test",quality_audit_timeout_seconds=.5 if mode=="timeout" else 2)
    provider=OpenAICompatibleLLMProvider(settings)
    await provider.client.aclose()
    provider.client=httpx.AsyncClient(base_url="https://example.test/",transport=httpx.MockTransport(handler))
    factory=async_sessionmaker(db.bind,expire_on_commit=False)
    try:
        await audit.run(factory,settings,provider,identity)
    finally: await provider.close()
    job=await db.get(GenerationJob,identity[0],populate_existing=True)
    assert job.quality_state == ("done" if mode in ("success","fail") else "failed")
    assert job.state=="succeeded" and job.result_json
    assert len(observed)==1 and observed[0]["max_tokens"]==100000


def test_migration_leaves_existing_jobs_unaudited(tmp_path):
    import importlib.util
    from pathlib import Path
    from sqlalchemy import create_engine, text
    from alembic.operations import Operations
    from alembic.migration import MigrationContext
    path=Path(__file__).parents[1]/"alembic/versions/0006_question_quality.py"
    spec=importlib.util.spec_from_file_location("quality_migration",path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine=create_engine("sqlite:///"+str(tmp_path/"quality.sqlite"))
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE generation_jobs (id INTEGER PRIMARY KEY, state TEXT, result_json TEXT)"))
        conn.execute(text("INSERT INTO generation_jobs VALUES (1, 'succeeded', '{}')"))
        with Operations.context(MigrationContext.configure(conn)): module.upgrade()
        assert conn.execute(text("SELECT state, result_json, quality_state, quality_attempts FROM generation_jobs")).one()==("succeeded","{}","not_requested",0)
    engine.dispose()


@pytest.mark.asyncio
async def test_teacher_can_explicitly_request_old_or_failed_audit_once(db):
    await prepare_seed(db)
    await complete_one(db,Settings(quality_audit_enabled=False))
    job=await db.scalar(select(GenerationJob).where(GenerationJob.state=="succeeded"))
    async def test_db(): yield db
    app.dependency_overrides[get_db]=test_db
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
            path=f"/api/admin/quality-audits/{job.id}/request"
            assert (await client.post(path)).status_code==401
            client.cookies.set("admin_session",create_token(str(uuid.uuid4()),"admin",username="teacher"))
            assert (await client.post(path)).status_code==200
            assert (await client.post(path)).status_code==409
            identity=await audit.claim(db)
            await audit.finish(db,identity,"mock",error="技术失败")
            assert (await client.post(path)).status_code==200
            assert job.state=="succeeded"
    finally: app.dependency_overrides.clear()

