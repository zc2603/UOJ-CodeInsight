import hashlib
import json
from sqlalchemy import select, func
from fastapi import HTTPException
from pydantic import BaseModel, Field, ConfigDict, ValidationError, field_validator
from app.models import Attempt, AttemptStatus, GenerationJob, QuizParticipant, Quiz
from app.schemas.llm import QuestionGenerationResult, LightweightGenerationResult, ChoiceOption
from app.config import get_settings


def revision(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class EditPreparedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: str = Field(min_length=64, max_length=64)
    question: str = Field(min_length=1, max_length=500)
    question_en: str = Field(min_length=1, max_length=800)
    reference_answer: str = Field(min_length=1, max_length=5000)
    grading_points: list[str] | None = Field(default=None, min_length=1, max_length=3)
    core_idea: str | None = Field(default=None, max_length=1000)
    choices: list[ChoiceOption] | None = None
    correct_choice_id: str | None = None

    @field_validator("question", "question_en", "reference_answer")
    @classmethod
    def not_blank(cls, value):
        if not value.strip(): raise ValueError("内容不能为空")
        return value.strip()

    @field_validator("grading_points")
    @classmethod
    def valid_points(cls, values):
        if values is None: return None
        if any(not v.strip() or len(v) > 5000 for v in values): raise ValueError("评分点不能为空或过长")
        return [v.strip() for v in values]


async def edit_prepared(db, quiz_id, student_number, job_id, index, payload):
    participant = await db.scalar(select(QuizParticipant).where(QuizParticipant.quiz_id == quiz_id,
        QuizParticipant.student_number == student_number).with_for_update())
    if participant is None: raise HTTPException(404, "学生不属于本场测评")
    quiz = await db.get(Quiz, quiz_id)
    if quiz.published_at is not None: raise HTTPException(409, "成绩已公布，不能编辑题目")
    latest = await db.scalar(select(Attempt).where(Attempt.participant_id == participant.id)
        .order_by(Attempt.attempt_no.desc()).limit(1))
    if latest and latest.status != AttemptStatus.RESET:
        raise HTTPException(409, "学生已开始作答，不能编辑题目")
    current_round = await db.scalar(select(func.max(GenerationJob.round_no)).where(GenerationJob.participant_id == participant.id))
    job = await db.scalar(select(GenerationJob).where(GenerationJob.id == job_id,
        GenerationJob.participant_id == participant.id).with_for_update().execution_options(populate_existing=True))
    if job is None: raise HTTPException(404, "题目不存在")
    if job.round_no != current_round or job.state != "succeeded" or revision(job.result_json) != payload.expected_revision:
        raise HTTPException(409, "题目已更新，请刷新后重新编辑")
    lightweight = quiz.assessment_version == "lightweight_v1"
    schema = LightweightGenerationResult if lightweight else QuestionGenerationResult
    generated = schema.model_validate(job.result_json)
    target = next((q for q in generated.questions if q.index == index), None)
    if target is None: raise HTTPException(404, "问题不存在")
    changes = payload.model_dump(exclude={"expected_revision"}, exclude_unset=True, mode="json")
    if not lightweight:
        changes = {key: value for key, value in changes.items() if key in {"question", "question_en", "reference_answer", "grading_points"}}
    else:
        changes.pop("grading_points", None)
        if target.response_format == "short_answer":
            changes.pop("choices", None)
            changes.pop("correct_choice_id", None)
        else:
            changes.pop("core_idea", None)
    updated = target.model_dump(mode="json") | changes
    result = generated.model_dump(mode="json")
    result["questions"] = [updated if q["index"] == index else q for q in result["questions"]]
    # An invalid edit (missing/duplicate options, answer key outside the options, ...) is
    # a client error; without this it escaped as a 500.
    try:
        job.result_json = schema.model_validate(result).model_dump(mode="json")
    except ValidationError as exc:
        raise HTTPException(422, "题目内容不合法：请检查选项与正确选项") from exc
    # A running audit of the old text must not publish a conclusion for this revision.
    job.quality_state = "not_requested" if not lightweight and get_settings().quality_audit_enabled else "paused"
    job.quality_token = None
    job.quality_lease_until = None
    job.quality_result = None
    job.quality_raw = None
    job.quality_error = None
    job.quality_acknowledged = None
    job.quality_finished_at = None
    await db.commit()
    return {"ok": True}
