import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from test_attempt_flow import db
from app.api.dependencies import AdminPrincipal
from app.config import Settings
from app.models import AdminUser, RuntimeConfiguration, RuntimeConfigurationAudit
from app.services import model_probe, runtime_settings
from app.services.runtime_settings import RuntimeOptions, RuntimeUpdate, save_runtime, runtime_response


def pending(service="openai", model="gpt-5.6-sol"):
    return dict(service=service, model=model, status="pending", message="testing",
        started_at=datetime.now(timezone.utc).isoformat())


def test_change_detection_including_standby_and_role_switch_deduplication():
    original = RuntimeOptions()
    assert model_probe.changed_services(original, RuntimeOptions(max_tokens=5000)) == []
    assert model_probe.changed_services(original, RuntimeOptions(openai_model="gpt-6-astra")) == ["openai"]
    assert model_probe.changed_services(original, RuntimeOptions(generation_service="openai", grading_service="openai")) == ["openai"]
    assert model_probe.changed_services(original, RuntimeOptions(deepseek_model="deepseek-pro", openai_model="gpt-6-astra")) == ["deepseek", "openai"]


@pytest.mark.asyncio
@pytest.mark.parametrize("service", ["deepseek", "openai"])
async def test_single_request_matches_model_protocol_and_small_budget(monkeypatch, service):
    calls = []
    settings = Settings(_env_file=None, llm_provider="openai-compatible", llm_api_key="synthetic-key",
        llm_base_url="https://synthetic.test/v1", llm_model="deepseek-pro" if service == "deepseek" else "gpt-6-astra",
        llm_api_style=service)
    async def respond(request):
        calls.append(request)
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer synthetic-key"
        body = json.loads(request.content)
        assert body["model"] == settings.llm_model
        assert body["messages"][1]["content"] == "1 + 1 等于多少？"
        assert body.get("max_tokens", body.get("max_completion_tokens")) == 2048
        assert ("thinking" in body) == (service == "deepseek")
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"answer":2}'}}]})
    monkeypatch.setattr(model_probe, "AsyncClient", lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(respond), **kw))
    result = await model_probe.probe(settings, pending(service, settings.llm_model))
    assert len(calls) == 1 and result["status"] == "passed"
    assert result["model"] == settings.llm_model and result["elapsed_ms"] >= 0
    assert "synthetic-key" not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,expected", [
    ("401", "HTTP 401"), ("429", "HTTP 429"), ("404", "HTTP 404"),
    ("invalid", "JSON"), ("wrong", "校验"), ("length", "额度不足"), ("oversized", "大小上限"), ("network", "网络连接失败"),
])
async def test_failures_are_sanitized_and_never_retried(monkeypatch, kind, expected):
    calls = []
    async def respond(request):
        calls.append(request)
        if kind.isdigit():
            return httpx.Response(int(kind), text="Bearer secret-body-must-not-leak")
        if kind == "network":
            raise httpx.ConnectError("secret-network-detail")
        if kind == "invalid":
            return httpx.Response(200, text="secret-invalid-body")
        if kind == "oversized":
            return httpx.Response(200, content=b"x" * 150000)
        return httpx.Response(200, json={"choices": [{"finish_reason": "length" if kind == "length" else "stop",
            "message": {"content": '{"answer":3}'}}]})
    monkeypatch.setattr(model_probe, "AsyncClient", lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(respond), **kw))
    settings = Settings(_env_file=None, llm_provider="openai-compatible", llm_api_key="synthetic-key")
    result = await model_probe.probe(settings, pending())
    assert len(calls) == 1 and result["status"] == "failed" and expected in result["message"]
    assert "secret" not in json.dumps(result)


@pytest.mark.asyncio
async def test_timeout_is_total_wall_clock_and_no_retry(monkeypatch):
    monkeypatch.setattr(model_probe, "PROBE_TIMEOUT_SECONDS", 0.02)
    calls = []
    async def respond(request):
        calls.append(request)
        await asyncio.sleep(1)
        return httpx.Response(200)
    monkeypatch.setattr(model_probe, "AsyncClient", lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(respond), **kw))
    result = await model_probe.probe(Settings(_env_file=None, llm_provider="openai-compatible", llm_api_key="synthetic"), pending())
    assert result["status"] == "failed" and "未完成" in result["message"]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_mock_and_missing_credentials_do_not_send_requests(monkeypatch):
    def forbidden(**kwargs):
        raise AssertionError("No client should be created")
    monkeypatch.setattr(model_probe, "AsyncClient", forbidden)
    assert (await model_probe.probe(Settings(_env_file=None, llm_provider="mock"), pending()))["status"] == "skipped"
    assert (await model_probe.probe(Settings(_env_file=None, llm_provider="openai-compatible", llm_api_key=""), pending()))["status"] == "failed"


def test_stale_pending_is_reported_as_incomplete_without_mutation():
    item = pending()
    item["started_at"] = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    assert model_probe.present_tests([item])[0]["status"] == "incomplete"
    assert item["status"] == "pending"


@pytest.mark.asyncio
async def test_save_persists_failure_reloads_and_duplicate_does_not_probe(db, monkeypatch):
    teacher = AdminUser(username="probe-teacher", password_hash="synthetic")
    db.add_all([teacher, RuntimeConfiguration(id=1, revision=0)])
    await db.commit()
    actor = AdminPrincipal(teacher.id, teacher.username)
    base = Settings(_env_file=None, llm_provider="openai-compatible", llm_api_key="synthetic-a", openai_api_key="synthetic-b")
    calls = []
    async def fail(settings, item):
        assert not db.in_transaction()
        calls.append(item["model"])
        return {**item, "status": "failed", "message": "HTTP 404：模型不存在"}
    monkeypatch.setattr(runtime_settings, "probe", fail)
    payload = RuntimeUpdate(expected_revision=0, settings=RuntimeOptions(generation_service="openai", grading_service="openai"))
    response = await save_runtime(db, base, actor, payload)
    assert response["revision"] == 1 and calls == ["gpt-5.6-sol"]
    assert response["model_tests"][0]["status"] == "failed"
    assert (await runtime_response(db, base))["model_tests"] == response["model_tests"]
    with pytest.raises(HTTPException) as exc:
        await save_runtime(db, base, actor, payload)
    assert exc.value.status_code == 409 and len(calls) == 1
    await db.rollback()
    await save_runtime(db, base, actor, RuntimeUpdate(expected_revision=1,
        settings=RuntimeOptions(generation_service="openai", grading_service="openai", max_tokens=9000)))
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_late_probe_never_overwrites_newer_settings(db, monkeypatch):
    teacher = AdminUser(username="probe-race", password_hash="synthetic")
    db.add_all([teacher, RuntimeConfiguration(id=1, revision=0)])
    await db.commit()
    actor = AdminPrincipal(teacher.id, teacher.username)
    base = Settings(_env_file=None, llm_provider="mock")
    started, finish = asyncio.Event(), asyncio.Event()
    async def slow(settings, item):
        started.set(); await finish.wait()
        return {**item, "status": "passed", "message": "ok"}
    monkeypatch.setattr(runtime_settings, "probe", slow)
    task = asyncio.create_task(save_runtime(db, base, actor, RuntimeUpdate(expected_revision=0,
        settings=RuntimeOptions(generation_service="openai"))))
    try:
        await asyncio.wait_for(started.wait(), 2)
        factory = async_sessionmaker(db.bind, expire_on_commit=False)
        async with factory() as newer:
            result = await save_runtime(newer, base, actor, RuntimeUpdate(expected_revision=1,
                settings=RuntimeOptions(generation_service="openai", generation_global_concurrency=4)))
            assert result["revision"] == 2 and result["model_tests"] == []
        finish.set()
        first = await task
        assert first["revision"] == 1 and first["model_tests"][0]["status"] == "passed"
        current = await runtime_response(db, base)
        assert current["revision"] == 2 and current["settings"].generation_global_concurrency == 4
        audits = (await db.scalars(select(RuntimeConfigurationAudit).order_by(RuntimeConfigurationAudit.revision))).all()
        assert audits[0].model_tests_json[0]["status"] == "passed" and audits[1].model_tests_json == []
    finally:
        finish.set()
        await asyncio.gather(task, return_exceptions=True)
