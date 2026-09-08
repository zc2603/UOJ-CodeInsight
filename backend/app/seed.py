from __future__ import annotations

import asyncio
import argparse
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.database import SessionLocal
from app.models import (
    Quiz,
    QuizParticipant,
    QuizParticipantStatus,
    QuizProblemSnapshot,
    QuizStatus,
    SubmissionSnapshot,
)
from app.security import hash_secret


SOURCES = [
    "#include <iostream>\nint main(){int n;std::cin>>n;std::cout<<n+1;}",
    "#include <vector>\nint main(){return 0;}",
    "def solve(n):\n    return sum(range(n + 1))\n",
    "#include <stdio.h>\nint main(void){puts(\"seed\");return 0;}",
]


async def seed(student_count: int = 5) -> None:
    now = datetime.now(timezone.utc)
    async with SessionLocal() as db:
        existing = (
            await db.execute(select(Quiz).where(Quiz.uoj_contest_id == 100))
        ).scalar_one_or_none()
        if existing:
            print(f"Seed Quiz already exists: {existing.id}")
            return
        quiz = Quiz(
            name="Seed Contest 100",
            uoj_contest_id=100,
            quiz_code_hash=hash_secret("SEED2026"),
            start_time=now - timedelta(hours=1),
            end_time=now + timedelta(days=7),
            duration_minutes=8,
            submission_cutoff=now,
            status=QuizStatus.PUBLISHED,
            show_score_after_finish=True,
        )
        db.add(quiz)
        await db.flush()
        problems = []
        for index in range(4):
            problem = QuizProblemSnapshot(
                quiz_id=quiz.id,
                uoj_problem_id=index + 1,
                title=f"Seed Problem {index + 1}",
                statement=f"虚拟题目 {index + 1}，用于本地完整流程测试。",
            )
            db.add(problem)
            problems.append(problem)
        await db.flush()
        for student_index in range(1, student_count + 1):
            participant = QuizParticipant(
                quiz_id=quiz.id,
                student_number=f"2026{student_index:04d}",
                eligible_problem_count=4,
                status=QuizParticipantStatus.READY,
            )
            db.add(participant)
            await db.flush()
            for problem_index, problem in enumerate(problems):
                db.add(
                    SubmissionSnapshot(
                        quiz_id=quiz.id,
                        participant_id=participant.id,
                        problem_snapshot_id=problem.id,
                        uoj_submission_id=1000 + student_index * 10 + problem_index,
                        uoj_problem_id=problem.uoj_problem_id,
                        source_code=SOURCES[problem_index],
                        language="Python3" if problem_index == 2 else "C++",
                        uoj_score=100 if problem_index % 2 == 0 else 70,
                        uoj_submit_time=now - timedelta(days=1),
                    )
                )
        await db.commit()
        print(f"Seed Quiz: {quiz.id}")
        print(f"Students: 20260001-2026{student_count:04d}")
        print("Quiz Code: SEED2026")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--students", type=int, default=5, choices=range(1, 501))
    args = parser.parse_args()
    asyncio.run(seed(args.students))
