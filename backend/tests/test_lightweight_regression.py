import asyncio
import json

import httpx
import pytest

from app.config import Settings
from app import lightweight_regression as runner
from app.schemas.llm import LightweightGenerationResult


def guard_report():
    return dict(http_requests=0, reserved_peak_cny=0.0, estimated_peak_cny=0.0, requests=[])


def request(**changes):
    payload = dict(model="deepseek-flash", max_tokens=4096)
    payload.update(changes)
    return httpx.Request("POST", "https://api.deepseek.com/chat/completions", json=payload)


@pytest.mark.asyncio
async def test_http_cap_atomic_and_size_model_limits():
    report = guard_report()
    guard = runner.RequestGuard(report, lambda: None)
    for _ in range(49): await guard.before(request())
    results = await asyncio.gather(guard.before(request()), guard.before(request()), return_exceptions=True)
    assert report["http_requests"] == 50
    assert sum(isinstance(result, runner.RegressionStop) for result in results) == 1
    for payload in (dict(model="unapproved"), dict(max_tokens=100000), dict(messages="x"*32768)):
        with pytest.raises(runner.RegressionStop): await guard.before(request(**payload))
    assert report["http_requests"] == 50
    assert report["reserved_peak_cny"] < runner.MAX_CNY


class TransportProvider(runner.GuardedProvider):
    handler = None
    def __init__(self, settings, guard):
        self.settings, self.guard, self.model_name = settings, guard, settings.llm_model
        self.semaphore = asyncio.Semaphore(1)
        self.client = httpx.AsyncClient(base_url="https://api.deepseek.com/",
            transport=httpx.MockTransport(type(self).handler),
            event_hooks={"request": [guard.before], "response": [guard.after]})


@pytest.mark.asyncio
@pytest.mark.parametrize("status,body,expected", [(401, {}, 1), (200, {"choices": [{"message": {"content": "{}"}}]}, 2)])
async def test_auth_and_two_schema_failures_stop_provider_retries(status, body, expected):
    report = guard_report()
    TransportProvider.handler = lambda req: httpx.Response(status, json=body)
    provider = TransportProvider(Settings(_env_file=None, llm_max_tokens=4096), runner.RequestGuard(report, lambda: None))
    try:
        with pytest.raises(runner.RegressionStop):
            await provider._request_json("test", "test", LightweightGenerationResult)
        assert report["http_requests"] == expected
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_fixed_scope_runs_once_and_counts_every_http(tmp_path):
    counter = 0
    def handler(req):
        nonlocal counter
        payload = json.loads(req.content)
        assert payload["max_tokens"] == 4096
        if counter < 4:
            kind = runner.CASES[counter]["kind"]
            system = payload["messages"][0]["content"]
            assert "JSON 字段约定" in system
            if kind:
                assert f'type="{kind}"，response_format="single_choice"' in system
            else:
                assert "本次仅生成第一道简答题" in system
            questions = [dict(index=1, type="explanation", response_format="short_answer", question="合成问题",
                question_en="Synthetic question", core_idea="合成理解目标", reference_answer="合成答案")]
            if kind:
                questions.append(dict(index=2, type=kind, response_format="single_choice", question="合成选择题",
                    question_en="Synthetic choice", reference_answer="A", correct_choice_id="A",
                    choices=[dict(id=c, text=c, text_en=c) for c in "ABCD"]))
            content = dict(schema_version="lightweight_v1", questions=questions)
        else:
            check = runner.fixed_checks()[counter-4]
            content = dict(grades=[dict(question_index=1, score=check[-2], reason="合成评分",
                confidence=.99, student_dispute=check[-1], needs_teacher_review=check[-1])])
        counter += 1
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(content)}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 100}})
    TransportProvider.handler = handler
    settings = Settings(_env_file=None, llm_provider="openai-compatible", llm_model="deepseek-flash")
    result = await runner.run(tmp_path / "first", settings=settings, provider_factory=TransportProvider)
    assert result["http_requests"] == counter == 10
    assert result["grading_passed"] and len(result["generation"]) == 4
    assert result["generation_semantic_review"] == "pending"
    assert (tmp_path / "first/report.json").exists()
    with pytest.raises(FileExistsError):
        await runner.run(tmp_path / "second", settings=settings, provider_factory=TransportProvider)
    assert counter == 10


@pytest.mark.asyncio
async def test_cost_reservation_blocks_before_dispatch():
    report = guard_report()
    report["reserved_peak_cny"] = runner.MAX_CNY
    guard = runner.RequestGuard(report, lambda: None)
    with pytest.raises(runner.RegressionStop): await guard.before(request())
    assert report["http_requests"] == 0
