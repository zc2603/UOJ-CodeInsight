import json
import asyncio
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app import lightweight_grading_diagnostic as diagnostic
from app import lightweight_regression as runner
from app.services.llm_provider import LLMProviderError


class TransportProvider(runner.GuardedProvider):
    handler = None

    def __init__(self, settings, guard):
        self.settings, self.guard, self.model_name = settings, guard, settings.llm_model
        self.semaphore = asyncio.Semaphore(1)
        self.client = httpx.AsyncClient(
            base_url="https://api.deepseek.com/",
            transport=httpx.MockTransport(type(self).handler),
            event_hooks={"request": [guard.before], "response": [guard.after]},
        )


def approved_settings():
    return Settings(
        _env_file=None,
        llm_provider="openai-compatible",
        llm_model="deepseek-flash",
        llm_base_url="https://api.deepseek.com",
        llm_max_tokens=100000,
    )


def grade_response(*, score=2, dispute=False, review=False):
    return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
        "grades": [{
            "question_index": 1,
            "score": score,
            "reason": "合成回答说明了短路保护。",
            "student_dispute": dispute,
            "needs_teacher_review": review,
            "confidence": 0.98,
        }],
    }, ensure_ascii=False)}}], "usage": {"prompt_tokens": 100, "completion_tokens": 100}}


@pytest.mark.parametrize("filename", [
    "question_generator_lightweight_v2.txt",
    "grader_lightweight_v1.txt",
])
def test_lightweight_json_mode_prompts_include_required_json_word(filename):
    prompt = (Path(__file__).parents[1] / "app" / "prompts" / filename).read_text(encoding="utf-8")
    assert "json" in prompt.lower()


@pytest.mark.asyncio
async def test_fixed_scope_is_one_case_three_http_attempts_and_within_budget(tmp_path):
    assert diagnostic.MAX_REQUESTS == diagnostic.MAX_CUMULATIVE_REQUESTS == 3
    assert diagnostic.CONCURRENCY == 1
    assert diagnostic.MAX_TOKENS == 100000
    assert diagnostic.RESERVE_PER_REQUEST == pytest.approx(0.867584)
    assert diagnostic.WORST_CASE_CNY == pytest.approx(2.602752)
    assert diagnostic.WORST_CASE_CNY < diagnostic.MAX_CNY == 3.00
    assert runner.fixed_checks()[0][0] == "colloquial_correct"

    captured = []

    def handler(req):
        payload = json.loads(req.content)
        captured.append(payload)
        assert payload["max_tokens"] == 100000
        assert "json" in payload["messages"][0]["content"].lower()
        assert "待评分简答题" in payload["messages"][1]["content"]
        return httpx.Response(200, request=req, json=grade_response())

    TransportProvider.handler = handler
    output = tmp_path / "first"
    report = await diagnostic.run(output, settings=approved_settings(), provider_factory=TransportProvider)

    assert report["http_requests"] == 1
    assert len(captured) == 1
    assert report["grading_passed"] is True
    assert report["completed"] is True
    assert "generation" not in report
    assert report["semantic_review"] == "pending"
    assert report["requests"][0]["status"] == 200
    assert report["requests"][0]["business_case"] == "grade:colloquial_correct"
    assert json.loads((output / "report.json").read_text(encoding="utf-8"))["http_requests"] == 1

    with pytest.raises(FileExistsError):
        await diagnostic.run(tmp_path / "second", settings=approved_settings(), provider_factory=TransportProvider)
    assert len(captured) == 1


@pytest.mark.asyncio
async def test_three_http_400_retries_stop_and_keep_only_sanitized_metadata(tmp_path, monkeypatch):
    async def no_wait(_seconds):
        return None

    monkeypatch.setattr("app.services.llm_provider.asyncio.sleep", no_wait)
    attempts = []

    def handler(req):
        attempts.append(req)
        return httpx.Response(400, request=req, json={"error": {
            "type": "invalid_request_error",
            "code": "unsupported_field",
            "message": "synthetic rejection; api_key=sk-0123456789abcdef0123456789",
            "debug_request": "must not be persisted",
        }})

    TransportProvider.handler = handler
    output = tmp_path / "rejected"
    with pytest.raises(LLMProviderError):
        await diagnostic.run(output, settings=approved_settings(), provider_factory=TransportProvider)

    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert len(attempts) == report["http_requests"] == 3
    assert [record["status"] for record in report["requests"]] == [400, 400, 400]
    assert all(record["provider_error"]["code"] == "unsupported_field" for record in report["requests"])
    serialized = json.dumps(report, ensure_ascii=False)
    assert "sk-0123456789abcdef0123456789" not in serialized
    assert "must not be persisted" not in serialized
    assert report["error_type"] == "LLMProviderError"
    assert report["completed"] is False


@pytest.mark.asyncio
async def test_custom_guard_blocks_fourth_attempt_before_dispatch():
    report = {"http_requests": 0, "reserved_peak_cny": 0.0, "estimated_peak_cny": 0.0, "requests": []}
    guard = runner.RequestGuard(
        report,
        lambda: None,
        max_requests=3,
        prior_requests=0,
        cumulative_http_cap=3,
        prior_estimated_cny=0,
        max_tokens=100000,
        max_body_bytes=32768,
        max_cny=3,
    )
    request = httpx.Request("POST", "https://api.deepseek.com/chat/completions", json={
        "model": "deepseek-flash", "max_tokens": 100000,
    })
    for _ in range(3):
        await guard.before(request)
    with pytest.raises(runner.RegressionStop, match="request cap"):
        await guard.before(request)
    assert report["http_requests"] == 3
