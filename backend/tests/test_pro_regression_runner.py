import json
from types import SimpleNamespace
import pytest
from app.config import Settings
from app.services.llm_provider import MockLLMProvider
from app.schemas.llm import GradingResult
from app import prompt_pro_regression as runner


@pytest.mark.asyncio
async def test_cross_topic_runner_offline(tmp_path,monkeypatch):
    class Fake(MockLLMProvider):
        model_name="deepseek-v4-pro"
        client=SimpleNamespace(event_hooks={})
        index=0
        async def grade_answers(self,**kwargs):
            expected=runner.fixed_checks()[self.index][3];self.index+=1
            result=GradingResult.model_validate({"grades":[dict(question_index=i,score=score,review_required=review,
                reason="synthetic",confidence=.9) for i,(score,review) in enumerate(expected,1)]})
            return result,result.model_dump_json()
        async def close(self):pass
    fake=Fake()
    monkeypatch.setattr(runner,"get_settings",lambda:Settings(llm_provider="openai-compatible",llm_model="deepseek-v4-pro"))
    monkeypatch.setattr(runner,"create_llm_provider",lambda settings:fake)
    await runner.run(tmp_path/"pro-run")
    report=json.loads((tmp_path/"pro-run/report.json").read_text(encoding="utf-8"))
    assert report["grading_passed"] and len(report["generation"])==3 and len(report["grading"])==4
