import json
from types import SimpleNamespace

import pytest
from app.config import Settings
from app.services.llm_provider import MockLLMProvider
from app.schemas.llm import GradingResult
from app import prompt_review_regression as runner


@pytest.mark.asyncio
async def test_real_runner_orchestration_offline(tmp_path, monkeypatch):
    class Fake(MockLLMProvider):
        model_name = "deepseek-v4-flash-vision-exp"
        client = SimpleNamespace(event_hooks={})
        generated = 0
        graded = 0
        async def generate_questions(self, **kwargs):
            self.generated += 1
            return await super().generate_questions(**kwargs)
        async def grade_answers(self, **kwargs):
            self.graded += 1
            if self.graded <= 3:
                return await super().grade_answers(**kwargs)
            score, review = [(1,False),(1,False),(1,False),(2,False),(2,True),(2,True),(0,False),(2,True)][self.graded-4]
            result = GradingResult.model_validate({"grades":[dict(question_index=1,score=score,reason="synthetic",confidence=.9,review_required=review)]})
            return result, result.model_dump_json()
        async def close(self): pass
    fake = Fake()
    monkeypatch.setattr(runner, "get_settings", lambda: Settings(llm_provider="openai-compatible", llm_model="deepseek-v4-flash-vision-exp"))
    monkeypatch.setattr(runner, "create_llm_provider", lambda settings: fake)
    await runner.run(tmp_path / "run")
    report = json.loads((tmp_path / "run/report.json").read_text(encoding="utf-8"))
    assert report["passed"] and fake.generated == 3 and fake.graded == 11
    assert all(c["status"] == "FINISHED" for c in report["cases"][:3])
