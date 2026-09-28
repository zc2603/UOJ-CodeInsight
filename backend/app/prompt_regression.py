"""Isolated service-flow regression. Real requests require explicit opt-in.

No UOJ access or production database writes. The SQLite file is retained.
Generated reference answers are smoke inputs, NOT an independent correctness oracle.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings, get_settings
from app.models import Attempt, Quiz, QuizParticipant, QuizParticipantStatus, QuizProblemSnapshot, QuizStatus, SubmissionSnapshot, Question
from app.models.base import Base
from app.services.llm_provider import create_llm_provider, LLMProviderError
from app.services.quiz_service import start_attempt
from app.services.question_service import submit_answer
from app.services.grading_service import grade_attempt


CASES = [
    ("array", "首行 n (1<=n<=4)，第二行 n 个整数，按原顺序输出。",
     '#include <iostream>\nusing namespace std; int main(){int n,a[4];cin>>n;for(int i=0;i<n;i++)cin>>a[i];for(int i=0;i<n;i++)cout<<a[i]<<" ";cout<<"\\n";}'),
    ("matrix", "首行固定为 2，后面两行各有两个整数，输出矩阵所有元素之和。",
     '#include <iostream>\nusing namespace std; int main(){int n,x,s=0;cin>>n;for(int i=0;i<n;i++)for(int j=0;j<n;j++){cin>>x;s+=x;}cout<<s<<" \\n";}'),
    ("two_matrices", "输入两个 2x2 矩阵，每个矩阵占两行，每行两个整数，没有尺寸行；输出两个矩阵所有元素之和。",
     '#include <iostream>\nusing namespace std; int main(){int a[2][2],b[2][2],s=0;for(int i=0;i<2;i++)for(int j=0;j<2;j++)cin>>a[i][j];for(int i=0;i<2;i++)for(int j=0;j<2;j++){cin>>b[i][j];s+=a[i][j]+b[i][j];}cout<<s<<"\\n";}'),
    ("function", "无标准输入。函数 sumPositive 返回数组中严格大于零的元素之和，数组长度至多 4。",
     'int sumPositive(const int *a,int n){int s=0;for(int i=0;i<n;i++)if(a[i]>0)s+=a[i];return s;}'),
]


async def run(output: Path, real: bool):
    output.mkdir(parents=True, exist_ok=False)
    settings = get_settings() if real else Settings(llm_provider="mock")
    if real and settings.llm_provider == "mock":
        raise ValueError("Real regression requires the configured real provider")
    settings = settings.model_copy(update={"llm_max_concurrency": 1, "llm_max_tokens": 12000})
    provider = create_llm_provider(settings)
    report = {"model": provider.model_name, "prompt_version": "v4", "grader_prompt_version": "v5", "http_requests": 0,
              "usage": [], "cases": [], "independent_semantic_review": "pending"}
    if real:
        async def before_request(request):
            if report["http_requests"] >= 12 or len(request.content) > 32000:
                raise LLMProviderError("Regression request budget exceeded")
            report["http_requests"] += 1
        async def after_response(response):
            await response.aread()
            try:
                report["usage"].append(response.json().get("usage", {}))
            except ValueError:
                pass
        provider.client.event_hooks = {"request": [before_request], "response": [after_response]}
    engine = create_async_engine("sqlite+aiosqlite:///" + (output / "regression.sqlite").as_posix())
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            for index, (name, statement, source) in enumerate(CASES, 1):
                now = datetime.now(timezone.utc)
                quiz = Quiz(name="Prompt v4 regression " + name, uoj_contest_id=0,
                    quiz_code_hash="unused-isolated-service-test", start_time=now,
                    end_time=now + timedelta(minutes=30), submission_cutoff=now, status=QuizStatus.PUBLISHED)
                db.add(quiz)
                await db.flush()
                problem = QuizProblemSnapshot(quiz_id=quiz.id, uoj_problem_id=index, title=name, statement=statement)
                participant = QuizParticipant(quiz_id=quiz.id, student_number=str(99000000 + index),
                    eligible_problem_count=1, status=QuizParticipantStatus.READY)
                db.add_all([problem, participant])
                await db.flush()
                db.add(SubmissionSnapshot(quiz_id=quiz.id, participant_id=participant.id,
                    problem_snapshot_id=problem.id, uoj_submission_id=index, uoj_problem_id=index,
                    source_code=source, language="C++", uoj_score=100, uoj_submit_time=now))
                await db.commit()
                quiz_id, student_number = quiz.id, participant.student_number
                started = await start_attempt(db, settings, provider, quiz_id=quiz.id,
                    student_number=participant.student_number, session_id="isolated-regression")
                assert started.question_count == 2
                questions = (await db.execute(select(Question).where(Question.attempt_id == started.attempt_id)
                    .order_by(Question.question_index))).scalars().all()
                record = {"name": name, "questions": [{"zh": q.question_text, "en": q.question_text_en,
                    "reference": q.reference_answer, "points": q.grading_points_json} for q in questions]}
                report["cases"].append(record)
                answers = [(q.question_index, q.reference_answer) for q in questions]
                for question_index, answer in answers:
                    await submit_answer(db, quiz_id=quiz_id, student_number=student_number,
                        session_id="isolated-regression", question_index=question_index,
                        student_answer=answer)
                db.expire_all()
                finished = await grade_attempt(db, provider, started.attempt_id)
                record["score"] = finished.auto_score
                record["status"] = finished.status.value
                print(f"case={name} status={finished.status.value} score={finished.auto_score}", flush=True)
            # Independent fixed oracle: output contains trailing spaces and newline,
            # but answer does not. Deliberately overstrict reference must be ignored.
            whitespace, raw = await provider.grade_answers(title="Output whitespace", statement=CASES[0][1],
                language="C++", source_code=CASES[0][2], question_payload=[{
                    "question_index": 1, "question": "输入：\n2\n1 2\n程序输出什么？",
                    "question_en": "Input:\n2\n1 2\nWhat does the program output?",
                    "reference_answer": "1 2 后有行末空格和最终换行。",
                    "grading_points": ["数值及顺序正确", "指出行末空格和最终换行"],
                    "student_answer": "1 2"}])
            report["whitespace_raw"] = raw
            report["whitespace_pass"] = whitespace.grades[0].score == 2
            if real:
                assert report["whitespace_pass"], "Whitespace regression failed"
    except Exception as exc:
        report["error_type"] = type(exc).__name__
        if getattr(exc, "raw_response", None):
            report["rejected_grading_raw"] = exc.raw_response
        raise
    finally:
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        await engine.dispose()
        close = getattr(provider, "close", None)
        if close:
            await close()


if __name__ == "__main__":
    # Retain the legacy v4 functions above for audit. The installed whitelist
    # entry point dispatches the bounded, explicitly approved lightweight run.
    from app.lightweight_regression import main
    main()
