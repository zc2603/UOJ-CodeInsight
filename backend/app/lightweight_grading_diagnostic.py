"""One authorized synthetic lightweight_v1 grading reproduction.

This module has no database or UOJ integration. A persistent exclusive claim
and an HTTP hook bound this one grading case to at most three requests total,
including provider retries.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path

from app.config import get_settings
from app.lightweight_regression import (
    CASES,
    INPUT_CNY_PER_M,
    OUTPUT_CNY_PER_M,
    PRICE_SOURCE,
    RequestGuard,
    RegressionStop,
    fixed_checks,
    GuardedProvider,
)
from app.services.llm_provider import (
    LIGHTWEIGHT_GENERATOR_VERSION,
    LIGHTWEIGHT_GRADER_VERSION,
)

SESSION = "20260928-lightweight-grade-repro-400-2"
MAX_REQUESTS = 3
MAX_CUMULATIVE_REQUESTS = 3
MAX_TOKENS = 100000
MAX_BODY_BYTES = 32768
MAX_CNY = 3.00
CONCURRENCY = 1
PRIOR_MODEL_REQUESTS = 14
PRIOR_ESTIMATED_CNY = 0.31382
PRICE_CHECKED = "2026-09-28"
RESERVE_PER_REQUEST = (
    (MAX_BODY_BYTES + 1024) * INPUT_CNY_PER_M + MAX_TOKENS * OUTPUT_CNY_PER_M
) / 1_000_000
WORST_CASE_CNY = RESERVE_PER_REQUEST * MAX_REQUESTS


async def run(
    output: Path,
    *,
    settings=None,
    provider_factory=GuardedProvider,
    claim: bool = True,
):
    settings = settings or get_settings()
    if (
        settings.llm_provider == "mock"
        or settings.llm_model != "deepseek-flash"
        or settings.llm_base_url.rstrip("/") != "https://api.deepseek.com"
    ):
        raise RegressionStop("Approved run requires the existing official Flash provider")
    if WORST_CASE_CNY > MAX_CNY:
        raise RegressionStop("Worst-case cost exceeds the fixed diagnostic budget")

    settings = settings.model_copy(update={
        "llm_max_concurrency": CONCURRENCY,
        "llm_max_tokens": MAX_TOKENS,
    })
    output.parent.mkdir(parents=True, exist_ok=True)
    if claim:
        claim_path = output.parent / f"{SESSION}.claim"
        with claim_path.open("x", encoding="utf-8") as handle:
            handle.write(str(output))
            handle.flush()
            os.fsync(handle.fileno())
    output.mkdir(parents=False, exist_ok=False)

    check_name, case_index, question, question_en, answer, expected_score, expected_dispute = fixed_checks()[0]
    case = CASES[case_index]
    report = dict(
        session=SESSION,
        model=settings.llm_model,
        generator=LIGHTWEIGHT_GENERATOR_VERSION,
        grader=LIGHTWEIGHT_GRADER_VERSION,
        case_name=check_name,
        case_kind=case["kind"],
        scope="one fixed synthetic grading case; no generation, database, or UOJ access",
        prior_model_requests=PRIOR_MODEL_REQUESTS,
        prior_estimated_peak_cny=PRIOR_ESTIMATED_CNY,
        prior_cost_is_outside_this_diagnostic_budget=True,
        additional_http_cap=MAX_REQUESTS,
        cumulative_http_cap=MAX_CUMULATIVE_REQUESTS,
        max_tokens=MAX_TOKENS,
        max_body_bytes=MAX_BODY_BYTES,
        concurrency=CONCURRENCY,
        max_cny=MAX_CNY,
        reserve_per_request_cny=RESERVE_PER_REQUEST,
        worst_case_cny=WORST_CASE_CNY,
        price_source=PRICE_SOURCE,
        price_checked=PRICE_CHECKED,
        expected_score=expected_score,
        expected_dispute=expected_dispute,
        http_requests=0,
        reserved_peak_cny=0.0,
        estimated_peak_cny=0.0,
        cumulative_estimated_peak_cny=0.0,
        requests=[],
        grading=[],
        grading_passed=None,
        semantic_review="pending",
        completed=False,
        started_at=time.time(),
    )

    def save():
        temp = output / "report.tmp"
        with temp.open("w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        temp.replace(output / "report.json")

    save()
    provider = None
    try:
        provider = provider_factory(settings, RequestGuard(
            report,
            save,
            max_requests=MAX_REQUESTS,
            prior_requests=0,
            cumulative_http_cap=MAX_CUMULATIVE_REQUESTS,
            prior_estimated_cny=0,
            max_tokens=MAX_TOKENS,
            max_body_bytes=MAX_BODY_BYTES,
            max_cny=MAX_CNY,
            input_cny_per_m=INPUT_CNY_PER_M,
            output_cny_per_m=OUTPUT_CNY_PER_M,
        ))
        report["active_case"] = "grade:" + check_name
        started = time.monotonic()
        result, _ = await provider.grade_lightweight(
            title=case["title"],
            statement=case["statement"],
            language="C++",
            source_code=case["source"],
            question_payload=[dict(
                question_index=1,
                question=question,
                question_en=question_en,
                student_answer=answer,
            )],
        )
        grade = result.grades[0]
        passed = (
            len(result.grades) == 1
            and grade.question_index == 1
            and grade.score == expected_score
            and grade.student_dispute == expected_dispute
            and grade.needs_teacher_review == expected_dispute
        )
        report["grading"].append(dict(
            name=check_name,
            seconds=time.monotonic() - started,
            passed=passed,
            result=result.model_dump(mode="json"),
        ))
        report["grading_passed"] = passed
        report["completed"] = True
        save()
    except Exception as exc:
        report["error_type"] = type(exc).__name__
        if isinstance(exc, RegressionStop):
            report["stop_reason"] = str(exc)
        raise
    finally:
        report["elapsed_seconds"] = time.time() - report["started_at"]
        save()
        if provider is not None:
            await provider.close()
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--confirm-real-cost", action="store_true")
    args = parser.parse_args()
    if not args.confirm_real_cost:
        parser.error("Explicit fixed-scope cost approval required")
    try:
        report = asyncio.run(run(args.output))
        print(
            "diagnostic=completed "
            f"requests={report['http_requests']} "
            f"grading_passed={report['grading_passed']} "
            "semantic_review=pending",
            flush=True,
        )
        if not report["grading_passed"]:
            raise SystemExit(2)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"diagnostic=stopped error_type={type(exc).__name__}", flush=True)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
