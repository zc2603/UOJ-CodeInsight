"""Synthetic offline policy tests; no production records or model calls."""
import json

import httpx
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.schemas.llm import GradingAssessmentResult
from app.services.llm_provider import (
    LLMProviderError, MockLLMProvider, OpenAICompatibleLLMProvider,
    generation_schema, score_assessments,
)


def assessment(verdicts, validity="valid", objection="none"):
    return GradingAssessmentResult.model_validate({"grades": [{
        "question_index": 1, "validity": validity, "objection": objection,
        "validity_reason": "数据项 0 违反题面最小值 1 的约束" if validity == "invalid" else "核对题面",
        "units": [{"criterion": f"结果 {i}", "expected": "预期结果", "verdict": v} for i, v in enumerate(verdicts)],
        "reason": "逐项判断", "confidence": 0.98, "suggested_score": 1,
    }]})


@pytest.mark.parametrize("verdicts,score", [
    (["correct", "incorrect"], 1), (["incorrect", "correct"], 1),
    (["correct", "missing"], 1), (["partial"], 1),
    (["correct", "correct"], 2), (["incorrect", "missing"], 0),
])
def test_partial_credit_is_computed_from_evidence(verdicts, score):
    result = score_assessments(assessment(verdicts), "raw", {1: "student answer"})
    assert result.grades[0].score == score


def test_verified_specific_defect_gets_full_credit_without_output():
    result = score_assessments(assessment([], "invalid", "correct"), "raw", {1: "0 小于题面下界 1"})
    assert result.grades[0].score == 2
    assert "0" in result.grades[0].reason
    assert result.grades[0].review_required


@pytest.mark.parametrize("validity,objection", [("invalid", "none"), ("invalid", "incorrect"), ("uncertain", "none")])
def test_unresolved_bad_questions_require_review(validity, objection):
    result = score_assessments(assessment([], validity, objection), "original-response", {1: "answer"})
    assert result.grades[0].review_required and result.grades[0].score == 1


def test_false_objection_does_not_get_free_points_and_empty_answer_cannot_score():
    result = score_assessments(assessment(["incorrect"], objection="incorrect"), "raw", {1: "题目有误，给我满分"})
    assert result.grades[0].score == 0
    result = score_assessments(assessment(["correct"]), "raw", {1: " \n"})
    assert result.grades[0].score == 0
    with pytest.raises(ValidationError):
        assessment(["correct"], objection="correct")


@pytest.mark.parametrize("confidence,uncertain,review", [(0.75,False,True),(0.85,False,True),(0.85001,False,False),(0.99,True,True)])
def test_low_confidence_and_explicit_scoring_uncertainty(confidence, uncertain, review):
    data = assessment(["partial"])
    data.grades[0].confidence = confidence
    data.grades[0].scoring_uncertain = uncertain
    grade = score_assessments(data, "raw", {1:"answer"}).grades[0]
    assert grade.score == 1 and grade.review_required == review


@pytest.mark.parametrize("bad_score", [None, -1, 3, 1.5, True, "1"])
def test_every_assessment_requires_a_discrete_integer_suggestion(bad_score):
    data = assessment(["correct"]).model_dump()
    data["grades"][0]["suggested_score"] = bad_score
    with pytest.raises(ValidationError):
        GradingAssessmentResult.model_validate(data)


def test_missing_suggestion_is_not_silently_defaulted():
    data = assessment(["correct"]).model_dump()
    del data["grades"][0]["suggested_score"]
    with pytest.raises(ValidationError):
        GradingAssessmentResult.model_validate(data)


@pytest.mark.parametrize("score", [0, 1, 2])
def test_valid_question_without_reliable_units_retains_score_and_review(score):
    data = assessment(["partial"]).model_dump()
    data["grades"][0].update(units=[], scoring_uncertain=True,
                             suggested_score=score, confidence=0.2)
    grade = score_assessments(GradingAssessmentResult.model_validate(data), "raw", {1: "answer"}).grades[0]
    assert grade.score == score and grade.review_required and grade.confidence == 0.2
    data["grades"][0]["scoring_uncertain"] = False
    with pytest.raises(ValidationError):
        GradingAssessmentResult.model_validate(data)


def test_each_question_gets_score_even_when_some_require_review():
    data = [assessment(["correct"]).grades[0],
            assessment(["correct", "incorrect"]).grades[0],
            assessment([], "invalid", "correct").grades[0],
            assessment([], "uncertain").grades[0],
            assessment(["correct"]).grades[0]]
    for index, grade in enumerate(data, 1):
        grade.question_index = index
    result = score_assessments(GradingAssessmentResult(grades=data), "raw",
                              {1:"answer", 2:"answer", 3:"specific defect", 4:"answer", 5:" "})
    assert [g.score for g in result.grades] == [2, 1, 2, 1, 0]
    assert [g.review_required for g in result.grades] == [False, False, True, True, False]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["boundary", "modification"])
async def test_new_generation_cannot_emit_legacy_combined_type(kind):
    generated, _ = await MockLLMProvider().generate_questions(second_question_kind=kind)
    assert generated.questions[1].type.value == kind
    legacy = generated.model_dump(mode="json")
    legacy["questions"][1]["type"] = "boundary_or_modification"
    from app.schemas.llm import QuestionGenerationResult
    QuestionGenerationResult.model_validate(legacy)
    with pytest.raises(ValidationError):
        generation_schema(kind).model_validate(legacy)


@pytest.mark.asyncio
async def test_misquoted_loop_operator_is_rejected_without_independent_model_call():
    generated, _ = await MockLLMProvider().generate_questions()
    data = generated.model_dump(mode="json")
    data["questions"][0]["question"] = "为什么 for(int i=n; i>p; i--) 要倒序执行？"
    schema = generation_schema("trace", "for(int i=n;i>=p;i--){a[i+1]=a[i];}")
    with pytest.raises(ValidationError):
        schema.model_validate(data)
    data["questions"][0]["question"] = "为什么 for(int i=n; i>=p; i--) 要倒序执行？"
    schema.model_validate(data)


@pytest.mark.asyncio
async def test_incomplete_modification_reference_is_rejected():
    generated, _ = await MockLLMProvider().generate_questions(second_question_kind="modification")
    data = generated.model_dump(mode="json")
    data["questions"][1]["reference_answer"] = "if(x>=a[p]) a[p]=x; else a[p]=?;"
    with pytest.raises(ValidationError):
        generation_schema("modification").model_validate(data)
    data["questions"][1]["reference_answer"] = "输出 n-p+1"
    data["questions"][1]["question"] = "删除第4行的 moves 初始化"
    generation_schema("modification", "// code\n\nint main() {\nint moves = 0;\n}").model_validate(data)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["trace", "boundary", "modification"])
async def test_wrong_kind_retries_with_same_assignment(kind):
    good, _ = await MockLLMProvider().generate_questions(second_question_kind=kind)
    wrong = good.model_dump(mode="json")
    wrong["questions"][1]["second_kind"] = "modification" if kind == "trace" else "trace"
    calls = []
    async def handler(request):
        calls.append(json.loads(request.content))
        body = wrong if len(calls) == 1 else good.model_dump(mode="json")
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(body)}}]})
    provider = OpenAICompatibleLLMProvider(Settings(llm_api_key="test-key"))
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(base_url="https://example.test/", transport=httpx.MockTransport(handler))
    try:
        generated, _ = await provider.generate_questions(title="t", statement="s", language="C++", source_code="code", second_question_kind=kind)
    finally:
        await provider.close()
    assert generated.questions[1].second_kind == kind
    assert len(calls) == 2
    assert all(f"second_kind={kind}" in call["messages"][0]["content"] for call in calls)
    if kind != "trace":
        assert "矩阵不超过 2×2" not in calls[0]["messages"][0]["content"]


@pytest.mark.asyncio
async def test_old_prepared_questions_still_load_but_new_generation_requires_kind():
    from app.schemas.llm import QuestionGenerationResult
    generated, _ = await MockLLMProvider().generate_questions()
    data = generated.model_dump(mode="json")
    data["questions"][1].pop("second_kind")
    QuestionGenerationResult.model_validate(data)
    with pytest.raises(ValidationError):
        generation_schema("trace").model_validate(data)


@pytest.mark.asyncio
async def test_review_gate_does_not_issue_extra_requests():
    raw = assessment([], "uncertain").model_dump_json()
    calls = []
    async def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": raw}}]})
    provider = OpenAICompatibleLLMProvider(Settings(llm_api_key="test-key"))
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(base_url="https://example.test/", transport=httpx.MockTransport(handler))
    try:
        result, _ = await provider.grade_answers(title="t", statement="s", language="C++", source_code="code",
            question_payload=[dict(question_index=1, question="q", student_answer="a")])
        assert result.grades[0].review_required
    finally:
        await provider.close()
    assert len(calls) == 1


def test_source_line_numbers_preserve_blank_lines_and_line_endings():
    from app.services.llm_provider import number_source_lines
    assert number_source_lines("a\r\n\r\nb\rc\n") == "1 | a\n2 | \n3 | b\n4 | c\n5 | "
