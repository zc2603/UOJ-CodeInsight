import json

import httpx
import pytest

from app import lightweight_grading_completion as completion
from app.config import Settings
from app.lightweight_regression import RegressionStop, RequestGuard
from app.schemas.llm import LightweightGradingResult


def approved_settings():
    return Settings(
        _env_file=None,
        llm_provider="openai-compatible",
        llm_model="deepseek-flash",
        llm_base_url="https://api.deepseek.com",
        llm_max_tokens=100000,
    )


class PassingProvider:
    def __init__(self, settings, guard):
        self.settings = settings
        self.guard = guard
        self.index = 0
        self.closed = False
        self.calls = []

    async def grade_lightweight(self, **kwargs):
        check = completion._selected_checks()[self.index]
        self.index += 1
        name, _, _, _, _, score, dispute = check
        self.calls.append(name)
        body = {
            "model": "deepseek-flash",
            "max_tokens": completion.MAX_TOKENS,
            "messages": [
                {"role": "system", "content": "输出合法 JSON 对象。"},
                {"role": "user", "content": "固定合成评分样例。"},
            ],
            "response_format": {"type": "json_object"},
        }
        request = httpx.Request("POST", "https://api.deepseek.com/chat/completions", json=body)
        await self.guard.before(request)
        result = LightweightGradingResult(grades=[{
            "question_index": 1,
            "score": score,
            "reason": "固定离线模拟结果。",
            "student_dispute": dispute,
            "dispute_reason": "明确表达异议。" if dispute else None,
            "needs_teacher_review": dispute,
            "review_reason": "显式异议应转教师复核。" if dispute else None,
            "confidence": 0.98,
        }])
        content = result.model_dump_json()
        response = httpx.Response(200, request=request, json={
            "choices": [{"finish_reason": "stop", "message": {"content": content}}],
            "usage": {"prompt_tokens": 600, "completion_tokens": 400},
        })
        self.guard.schema = LightweightGradingResult
        await self.guard.after(response)
        return result, content

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_runs_only_the_five_remaining_synthetic_cases_under_approved_caps(tmp_path, monkeypatch):
    assert completion._selected_checks()
    assert completion.MAX_REQUESTS == completion.MAX_CUMULATIVE_REQUESTS == 25
    assert completion.CONCURRENCY == 1
    assert completion.MAX_TOKENS == 100000
    assert completion.MAX_BODY_BYTES == 32768
    assert completion.RESERVE_PER_REQUEST == pytest.approx(0.867584)
    assert completion.WORST_CASE_CNY == pytest.approx(21.6896)
    assert completion.WORST_CASE_CNY < completion.MAX_CNY == 22.00

    created = []

    def provider_factory(settings, guard):
        provider = PassingProvider(settings, guard)
        created.append(provider)
        return provider

    monkeypatch.setattr(completion, "CLAIM_PATH", tmp_path / f"{completion.SESSION}.claim")
    output = tmp_path / "completion"
    report = await completion.run(
        output,
        settings=approved_settings(),
        provider_factory=provider_factory,
    )

    assert created[0].calls == list(completion.CHECK_NAMES)
    assert created[0].closed
    assert report["grading_passed"] is True
    assert report["completed"] is True
    assert report["http_requests"] == 5
    assert [item["name"] for item in report["grading"]] == list(completion.CHECK_NAMES)
    assert all(item["passed"] for item in report["grading"])
    assert report["semantic_review"] == "pending"
    assert report["reused_passing_coverage"]["generation_prompt_unchanged"] is True
    assert completion.CLAIM_PATH.is_file()
    assert json.loads((output / "report.json").read_text(encoding="utf-8"))["http_requests"] == 5

    with pytest.raises(FileExistsError):
        await completion.run(
            tmp_path / "replay",
            settings=approved_settings(),
            provider_factory=provider_factory,
        )
    assert len(created) == 1


@pytest.mark.asyncio
async def test_completion_http_guard_blocks_request_26_before_dispatch():
    report = {"http_requests": 0, "reserved_peak_cny": 0.0, "estimated_peak_cny": 0.0, "requests": []}
    guard = RequestGuard(
        report,
        lambda: None,
        max_requests=completion.MAX_REQUESTS,
        prior_requests=0,
        cumulative_http_cap=completion.MAX_CUMULATIVE_REQUESTS,
        prior_estimated_cny=0,
        max_tokens=completion.MAX_TOKENS,
        max_body_bytes=completion.MAX_BODY_BYTES,
        max_cny=completion.MAX_CNY,
    )
    request = httpx.Request("POST", "https://api.deepseek.com/chat/completions", json={
        "model": "deepseek-flash", "max_tokens": completion.MAX_TOKENS,
    })
    for _ in range(completion.MAX_REQUESTS):
        await guard.before(request)
    with pytest.raises(RegressionStop, match="request cap"):
        await guard.before(request)
    assert report["http_requests"] == completion.MAX_REQUESTS


@pytest.mark.asyncio
async def test_completion_cost_guard_rejects_before_dispatch(tmp_path):
    report = {"http_requests": 0, "reserved_peak_cny": 0.0, "estimated_peak_cny": 0.0, "requests": []}
    guard = RequestGuard(
        report,
        lambda: None,
        max_requests=completion.MAX_REQUESTS,
        prior_requests=0,
        cumulative_http_cap=completion.MAX_CUMULATIVE_REQUESTS,
        prior_estimated_cny=0,
        max_tokens=completion.MAX_TOKENS,
        max_body_bytes=completion.MAX_BODY_BYTES,
        max_cny=completion.RESERVE_PER_REQUEST - 0.001,
    )
    request = httpx.Request("POST", "https://api.deepseek.com/chat/completions", json={
        "model": "deepseek-flash", "max_tokens": completion.MAX_TOKENS,
    })
    with pytest.raises(RegressionStop, match="Cost cap"):
        await guard.before(request)
    assert report["http_requests"] == 0
    assert report["requests"] == []
