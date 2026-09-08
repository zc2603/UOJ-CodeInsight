"""Basic concurrent end-to-end load test for a mock-provider Quiz."""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time

import httpx


async def run_student(
    base_url: str,
    quiz_id: str,
    quiz_code: str,
    student_number: str,
    gate: asyncio.Semaphore,
) -> float:
    started = time.perf_counter()
    async with gate, httpx.AsyncClient(base_url=base_url, timeout=180) as client:
        response = await client.post(
            f"/api/quiz/{quiz_id}/login",
            json={"student_number": student_number, "quiz_code": quiz_code},
        )
        response.raise_for_status()
        response = await client.post(f"/api/quiz/{quiz_id}/start")
        response.raise_for_status()
        question = response.json()
        while question.get("question_index") is not None:
            response = await client.post(
                "/api/attempt/current/answer",
                json={
                    "question_index": question["question_index"],
                    "answer": "这是并发压测使用的完整模拟回答，包含足够长度并结合代码流程说明。",
                },
            )
            response.raise_for_status()
            question = response.json()
            if question.get("submitted"):
                break
        response = await client.get("/api/attempt/current/result")
        response.raise_for_status()
    return time.perf_counter() - started


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8090")
    parser.add_argument("--quiz-id", required=True)
    parser.add_argument("--quiz-code", required=True)
    parser.add_argument("--student-prefix", default="2026")
    parser.add_argument("--start-index", type=int, default=1)
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=100)
    parser.add_argument(
        "--confirm-llm-cost",
        action="store_true",
        help="required acknowledgement because each fresh Attempt makes two model calls",
    )
    args = parser.parse_args()
    if not args.confirm_llm_cost:
        parser.error("pass --confirm-llm-cost after confirming Mock or accepting model cost")

    students = [
        f"{args.student_prefix}{index:04d}"
        for index in range(args.start_index, args.start_index + args.count)
    ]
    gate = asyncio.Semaphore(args.concurrency)
    wall_started = time.perf_counter()
    results = await asyncio.gather(
        *(
            run_student(
                args.base_url.rstrip("/"), args.quiz_id, args.quiz_code, student, gate
            )
            for student in students
        ),
        return_exceptions=True,
    )
    wall = time.perf_counter() - wall_started
    failures = [item for item in results if isinstance(item, BaseException)]
    durations = sorted(float(item) for item in results if not isinstance(item, BaseException))
    if durations:
        p95 = durations[min(len(durations) - 1, round(len(durations) * 0.95) - 1)]
        print(
            f"completed={len(durations)} failed={len(failures)} wall={wall:.2f}s "
            f"mean={statistics.mean(durations):.2f}s p95={p95:.2f}s max={max(durations):.2f}s"
        )
    for failure in failures[:10]:
        print(f"failure: {failure!r}")
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
