import hashlib
import json
from sqlalchemy import select, func
from fastapi import HTTPException
from pydantic import BaseModel, Field, ConfigDict, field_validator
from app.models import Attempt, AttemptStatus, GenerationJob, QuizParticipant
from app.schemas.llm import QuestionGenerationResult


def revision(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class EditPreparedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: str = Field(min_length=64, max_length=64)
    question: str = Field(min_length=1, max_length=500)
    question_en: str = Field(min_length=1, max_length=800)
    reference_answer: str = Field(min_length=1, max_length=5000)
    grading_points: list[str] = Field(min_length=1, max_length=3)

    @field_validator("question", "question_en", "reference_answer")
    @classmethod
    def not_blank(cls, value):
        if not value.strip(): raise ValueError("内容不能为空")
        return value.strip()

    @field_validator("grading_points")
    @classmethod
    def valid_points(cls, values):
        if any(not v.strip() or len(v) > 5000 for v in values): raise ValueError("评分点不能为空或过长")
        return [v.strip() for v in values]


async def edit_prepared(db, quiz_id, student_number, job_id, index, payload):
    participant = await db.scalar(select(QuizParticipant).where(QuizParticipant.quiz_id == quiz_id,
        QuizParticipant.student_number == student_number).with_for_update())
    if participant is None: raise HTTPException(404, "学生不属于本场测评")
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
    generated = QuestionGenerationResult.model_validate(job.result_json)
    target = next((q for q in generated.questions if q.index == index), None)
    if target is None: raise HTTPException(404, "问题不存在")
    updated = target.model_dump(mode="json") | payload.model_dump(exclude={"expected_revision"})
    result = generated.model_dump(mode="json")
    result["questions"] = [updated if q["index"] == index else q for q in result["questions"]]
    job.result_json = QuestionGenerationResult.model_validate(result).model_dump(mode="json")
    # A running audit of the old text must not publish a conclusion for this revision.
    job.quality_state = "not_requested"
    job.quality_token = None
    job.quality_lease_until = None
    job.quality_result = None
    job.quality_raw = None
    job.quality_error = None
    job.quality_acknowledged = None
    job.quality_finished_at = None
    await db.commit()
    return {"ok": True}
