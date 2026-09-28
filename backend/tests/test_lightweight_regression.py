import asyncio
import json

import httpx
import pytest

from app.config import Settings
from app import lightweight_regression as runner
from app.schemas.llm import LightweightGenerationResult
from app.services.llm_provider import LLMProviderError


def guard_report():
    return dict(http_requests=0, reserved_peak_cny=0.0, estimated_peak_cny=0.0, requests=[])


def request(**changes):
    payload = dict(model="deepseek-flash", max_tokens=runner.MAX_TOKENS)
    payload.update(changes)
    return httpx.Request("POST", "https://api.deepseek.com/chat/completions", json=payload)


@pytest.mark.asyncio
async def test_http_cap_atomic_and_size_model_limits():
    assert runner.PRIOR_REQUESTS + runner.MAX_REQUESTS == runner.MAX_CUMULATIVE_REQUESTS
    assert runner.RESERVE_PER_REQUEST == pytest.approx(0.867584)
    assert runner.PRIOR_ESTIMATED_CNY + runner.MAX_REQUESTS * runner.RESERVE_PER_REQUEST == pytest.approx(37.506372)
    report = guard_report()
    guard = runner.RequestGuard(report, lambda: None)
    for _ in range(runner.MAX_REQUESTS - 1): await guard.before(request())
    results = await asyncio.gather(guard.before(request()), guard.before(request()), return_exceptions=True)
    assert report["http_requests"] == runner.MAX_REQUESTS
    assert sum(isinstance(result, runner.RegressionStop) for result in results) == 1
    for payload in (dict(model="unapproved"), dict(max_tokens=100000), dict(messages="x"*32768)):
        with pytest.raises(runner.RegressionStop): await guard.before(request(**payload))
    assert report["http_requests"] == runner.MAX_REQUESTS
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
    provider = TransportProvider(Settings(_env_file=None, llm_max_tokens=runner.MAX_TOKENS),
        runner.RequestGuard(report, lambda: None))
    try:
        with pytest.raises(runner.RegressionStop):
            await provider._request_json("test", "test", LightweightGenerationResult)
        assert report["http_requests"] == expected
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_http_errors_save_only_redacted_bounded_provider_metadata():
    report = guard_report()
    guard = runner.RequestGuard(report, lambda: None)
    req = request()
    await guard.before(req)
    response = httpx.Response(400, request=req, json={"error": {
        "type": "invalid_request_error", "code": "bad_parameter",
        "message": "Authorization: Bearer local-test-secret-token; api_key=sk-0123456789abcdef0123456789 " + "x" * 400,
        "debug_request": "should never be saved",
    }})

    await guard.after(response)

    record = report["requests"][0]
    assert record["status"] == 400
    assert record["provider_error"]["type"] == "invalid_request_error"
    assert record["provider_error"]["code"] == "bad_parameter"
    assert len(record["provider_error"]["message"]) <= runner.MAX_ERROR_TEXT_CHARS
    serialized = json.dumps(record, ensure_ascii=False)
    assert "local-test-secret-token" not in serialized
    assert "sk-0123456789abcdef0123456789" not in serialized
    assert "should never be saved" not in serialized
    assert "raw_response" not in record and "body" not in record


@pytest.mark.asyncio
async def test_http_400_retry_metadata_is_saved_for_each_request(monkeypatch):
    async def no_wait(_seconds):
        return None
    monkeypatch.setattr("app.services.llm_provider.asyncio.sleep", no_wait)
    report = guard_report()
    body = {"error": {"type": "invalid_request_error", "code": "unsupported_field",
        "message": "field rejected; api_key=sk-0123456789abcdef0123456789"}}
    TransportProvider.handler = lambda req: httpx.Response(400, request=req, json=body)
    provider = TransportProvider(Settings(_env_file=None, llm_max_tokens=runner.MAX_TOKENS),
        runner.RequestGuard(report, lambda: None))
    try:
        with pytest.raises(LLMProviderError):
            await provider._request_json("synthetic system", "synthetic user", LightweightGenerationResult)
        assert report["http_requests"] == 3
        assert [item["status"] for item in report["requests"]] == [400, 400, 400]
        assert all(item["provider_error"]["code"] == "unsupported_field" for item in report["requests"])
        assert all("sk-0123456789abcdef0123456789" not in json.dumps(item) for item in report["requests"])
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_generation_and_grading_share_the_same_request_envelope():
    captured = []
    generation_content = {"schema_version": "lightweight_v1", "questions": [{
        "index": 1, "type": "explanation", "response_format": "short_answer",
        "question": "合成问题", "question_en": "Synthetic question",
        "reference_answer": "合成答案", "core_idea": "合成机制",
    }]}
    grading_content = {"grades": [{"question_index": 1, "score": 2, "reason": "回答正确",
        "student_dispute": False, "needs_teacher_review": False, "confidence": 0.99}]}
    def handler(req):
        payload = json.loads(req.content)
        captured.append((req.url, payload))
        content = grading_content if "待评分简答题" in payload["messages"][1]["content"] else generation_content
        return httpx.Response(200, request=req, json={"choices": [{"finish_reason": "stop",
            "message": {"content": json.dumps(content, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 100}})
    TransportProvider.handler = handler
    settings = Settings(_env_file=None, llm_provider="openai-compatible", llm_model="deepseek-flash",
        llm_max_tokens=runner.MAX_TOKENS)
    provider = TransportProvider(settings, runner.RequestGuard(guard_report(), lambda: None))
    try:
        await provider.generate_lightweight(title="合成题", statement="合成题面", language="C++",
            source_code="int main() {}", second_question_kind=None)
        await provider.grade_lightweight(title="合成题", statement="合成题面", language="C++",
            source_code="int main() {}", question_payload=[{"question_index": 1, "question": "为什么？",
                "question_en": "Why?", "student_answer": "因为这是合成答案。"}])
    finally:
        await provider.close()

    assert len(captured) == 2
    assert captured[0][0] == captured[1][0]
    generation_request, grading_request = (item[1] for item in captured)
    assert set(generation_request) == set(grading_request) == {
        "model", "messages", "thinking", "reasoning_effort", "max_tokens", "response_format",
    }
    for field in ("model", "thinking", "reasoning_effort", "max_tokens", "response_format"):
        assert generation_request[field] == grading_request[field]
    assert generation_request["max_tokens"] == runner.MAX_TOKENS == 100000
    assert generation_request["messages"] != grading_request["messages"]


@pytest.mark.asyncio
async def test_fixed_scope_runs_once_and_counts_every_http(tmp_path):
    assert [(case["name"], case["kind"]) for case in runner.CASES[:3]] == [
        ("loop_short_circuit", "trace"), ("array_stack", "boundary"),
        ("linked_state", None),
    ]
    assert runner.CASES[3]["name"] == "flawed_valid_submission"
    assert runner.CASES[3]["kind"] == "modification"
    counter = 0
    def handler(req):
        nonlocal counter
        payload = json.loads(req.content)
        assert payload["max_tokens"] == runner.MAX_TOKENS
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
    settings = Settings(_env_file=None, llm_provider="openai-compatible", llm_model="deepseek-flash",
        llm_max_tokens=runner.MAX_TOKENS)
    result = await runner.run(tmp_path / "first", settings=settings, provider_factory=TransportProvider)
    assert result["http_requests"] == counter == 10
    assert result["prior_http_requests"] == 7
    assert result["cumulative_http_cap"] == runner.MAX_CUMULATIVE_REQUESTS
    assert result["additional_http_cap"] == 43
    assert result["max_tokens"] == 100000
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


@pytest.mark.asyncio
async def test_recheck_never_exceeds_cumulative_request_or_cost_scope():
    report = guard_report()
    report["http_requests"] = runner.MAX_REQUESTS
    guard = runner.RequestGuard(report, lambda: None)
    with pytest.raises(runner.RegressionStop): await guard.before(request())
    assert report["http_requests"] == runner.MAX_REQUESTS

    report = guard_report()
    report["reserved_peak_cny"] = runner.MAX_CNY - runner.PRIOR_ESTIMATED_CNY
    guard = runner.RequestGuard(report, lambda: None)
    with pytest.raises(runner.RegressionStop): await guard.before(request())
    assert report["http_requests"] == 0
