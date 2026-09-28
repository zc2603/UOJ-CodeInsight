"""One explicitly approved synthetic lightweight_v1 run; no database access.

Authorization is scoped to SESSION, with an exclusive persistent claim. Neither
restarting the command nor provider retries can silently obtain another budget.
Only synthetic model content and usage are saved; never headers or credentials.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path

from pydantic import ValidationError

from app.config import get_settings
from app.services.llm_provider import (
    OpenAICompatibleLLMProvider, LIGHTWEIGHT_GENERATOR_VERSION, LIGHTWEIGHT_GRADER_VERSION,
)

SESSION = "20260928-lightweight-v2-confirmed-recheck"
PRIOR_REQUESTS = 2
PRIOR_ESTIMATED_CNY = 0.040548
MAX_REQUESTS = 48  # Additional requests; cumulative session ceiling is 50.
MAX_TOKENS = 4096
MAX_BODY_BYTES = 32768
MAX_CNY = 100
# Official CNY peak rates verified 2026-09-28. Ignore cache discounts for bounds.
INPUT_CNY_PER_M = 2
OUTPUT_CNY_PER_M = 8
PRICE_SOURCE = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"
RESERVE_PER_REQUEST = ((MAX_BODY_BYTES + 1024) * INPUT_CNY_PER_M + MAX_TOKENS * OUTPUT_CNY_PER_M) / 1_000_000

CASES = [
    dict(name="loop_short_circuit", kind="trace", title="首个正数", statement="输入 n（1≤n≤6）及 n 个整数（−9≤a[i]≤9）。输出第一个正数的下标（从0开始）；不存在时输出−1。", source='''#include <iostream>
using namespace std;
int main() {
    int n, a[6]; cin >> n;
    for (int j = 0; j < n; ++j) cin >> a[j];
    int i = 0;
    while (i < n && a[i] <= 0) ++i;
    cout << (i < n ? i : -1);
}'''),
    dict(name="array_stack", kind="boundary", title="括号匹配", statement="输入长度1到8、仅由左右圆括号组成的字符串，完全匹配输出YES，否则输出NO。", source='''#include <iostream>
#include <string>
using namespace std;
int main() {
    string t; cin >> t;
    char st[8]; int top = 0; bool ok = true;
    for (char c : t) {
        if (c == '(') st[top++] = c;
        else {
            if (top == 0) { ok = false; break; }
            --top;
        }
    }
    cout << (ok && top == 0 ? "YES" : "NO");
}'''),
    # Third ordered synthetic problem: the highest problem_id gets one
    # explanation question and no second question.
    dict(name="linked_state", kind=None, title="删除链表中的首个指定值", statement="初始链表为2→4→2→7，以数组记录值和后继，0代表空节点。输入整数x（1≤x≤9），删除第一个值为x的节点，不存在则保持原链表。按顺序输出剩余节点的值。", source='''#include <iostream>
using namespace std;
int main() {
    int value[5] = {0, 2, 4, 2, 7};
    int next[5] = {0, 2, 3, 4, 0};
    int head = 1, prev = 0, cur = head, x; cin >> x;
    while (cur != 0 && value[cur] != x) { prev = cur; cur = next[cur]; }
    if (cur != 0) {
        if (prev == 0) head = next[cur];
        else next[prev] = next[cur];
    }
    for (int p = head; p != 0; p = next[p]) cout << value[p] << ' ';
}'''),
    # Auxiliary type-coverage case; it is not assigned a problem_id/order.
    dict(name="flawed_valid_submission", kind="modification", title="数组最大值", statement="输入 n（1≤n≤5）及 n 个整数（−9≤a[i]≤9），输出数组最大值。该提交存在缺陷，但源码可分析，部分合法输入可得到正确结果。", source='''#include <iostream>
using namespace std;
int main() {
    int n, a[5]; cin >> n;
    for (int i = 0; i < n; ++i) cin >> a[i];
    int best = 0;
    for (int i = 0; i < n; ++i) if (a[i] > best) best = a[i];
    cout << best;
}'''),
]


def fixed_checks():
    loop_q = "为什么 while 条件必须先判断 i < n，再判断 a[i] <= 0？"
    stack_q = "最后判断括号匹配时，为什么必须同时检查 ok 和 top == 0？"
    return [
        ("colloquial_correct", 0, loop_q, "Why must i < n be checked before a[i] <= 0 in the while condition?", "先看看下标有没有走到头，走到头就停，后面那格就不读了，免得读出数组范围。", 2, False),
        ("equivalent_correct", 0, loop_q, "Why must i < n be checked before a[i] <= 0 in the while condition?", "当 i 等于 n，左边为假，&& 短路使右边不再求值，因而不会访问不存在的 a[n]。", 2, False),
        ("missing_key_understanding", 1, stack_q, "Why must the final bracket-matching test check both ok and top == 0?", "top 等于0说明栈里已经没有没配对的左括号了。", 1, False),
        ("core_error", 1, stack_q, "Why must the final bracket-matching test check both ok and top == 0?", "只要栈是空的就一定完全匹配，ok 是多余的，可以去掉。", 0, False),
        ("explicit_dispute", 3, "为什么将 best 初始化为0能保证全负数组的最大值也计算正确？", "Why does initializing best to 0 guarantee the correct maximum even when all elements are negative?", "题目的前提不成立。全是负数时没有元素大于0，best一直是0，它不是数组最大值，应当从第一个元素初始化。", 2, True),
        ("ordinary_correct", 2, "删除首个匹配节点时，prev 起什么作用？", "What role does prev play when deleting the first matching node?", "prev 记录当前节点的前驱。删除时把前驱的后继改成当前节点的后继，就把当前节点跳过去；如果 prev 为0，则当前节点是头，直接移动 head。", 2, False),
    ]


class RegressionStop(RuntimeError):
    """Deliberately not caught by the provider's HTTP/schema retry branches."""


class RequestGuard:
    def __init__(self, report, save):
        self.report, self.save = report, save
        self.lock = asyncio.Lock()
        self.schema = None
        self.consecutive_structure_errors = 0

    async def before(self, request):
        async with self.lock:
            body = json.loads(request.content)
            if (request.url.host != "api.deepseek.com" or request.url.scheme != "https"
                or body.get("model") != "deepseek-flash" or body.get("max_tokens") != MAX_TOKENS
                or len(request.content) > MAX_BODY_BYTES):
                raise RegressionStop("Request outside approved model, endpoint or size")
            if (self.report["http_requests"] >= MAX_REQUESTS
                or PRIOR_REQUESTS + self.report["http_requests"] >= 50):
                raise RegressionStop("HTTP request cap reached")
            if (PRIOR_ESTIMATED_CNY + self.report["reserved_peak_cny"] + RESERVE_PER_REQUEST
                > MAX_CNY):
                raise RegressionStop("Cost cap reached")
            self.report["http_requests"] += 1
            self.report["reserved_peak_cny"] += RESERVE_PER_REQUEST
            self.report["requests"].append(dict(index=self.report["http_requests"],
                business_case=self.report.get("active_case"), input_bytes=len(request.content),
                reserved_peak_cny=RESERVE_PER_REQUEST, sent_at=time.time()))
            # Durable reservation is written before dispatch, including retries.
            self.save()

    async def after(self, response):
        await response.aread()
        record = self.report["requests"][-1]
        record["status"] = response.status_code
        record["elapsed_seconds"] = time.time() - record["sent_at"]
        try:
            body = response.json()
        except ValueError:
            body = {}
        record["usage"] = body.get("usage", {}) if isinstance(body, dict) else {}
        usage = record["usage"] or {}
        record["estimated_peak_cny"] = (usage.get("prompt_tokens", 0) * INPUT_CNY_PER_M
            + usage.get("completion_tokens", 0) * OUTPUT_CNY_PER_M) / 1_000_000
        self.report["estimated_peak_cny"] = sum(r.get("estimated_peak_cny", 0) for r in self.report["requests"])
        self.report["cumulative_estimated_peak_cny"] = (
            PRIOR_ESTIMATED_CNY + self.report["estimated_peak_cny"])
        if response.status_code in (401, 402, 403):
            self.save()
            raise RegressionStop("Authentication or billing failure")
        if response.is_success:
            try:
                choice = body["choices"][0]
                record["finish_reason"] = choice.get("finish_reason")
                record["content"] = choice["message"]["content"]
                self.schema.model_validate_json(record["content"])
                self.consecutive_structure_errors = 0
            except (KeyError, IndexError, TypeError, ValidationError):
                self.consecutive_structure_errors += 1
                record["structure_error"] = True
        self.save()
        if self.consecutive_structure_errors >= 2:
            raise RegressionStop("Two consecutive structure errors")
        if self.report["cumulative_estimated_peak_cny"] >= MAX_CNY:
            raise RegressionStop("Actual estimated cost cap reached")


class GuardedProvider(OpenAICompatibleLLMProvider):
    def __init__(self, settings, guard):
        super().__init__(settings)
        self.guard = guard
        self.client.event_hooks = {"request": [guard.before], "response": [guard.after]}

    async def _request_json(self, system_prompt, user_prompt, schema):
        self.guard.schema = schema
        return await super()._request_json(system_prompt, user_prompt, schema)


async def run(output: Path, *, settings=None, provider_factory=GuardedProvider, claim=True):
    settings = settings or get_settings()
    if settings.llm_provider == "mock" or settings.llm_model != "deepseek-flash" or settings.llm_base_url.rstrip("/") != "https://api.deepseek.com":
        raise RegressionStop("Approved run requires the existing official Flash provider")
    settings = settings.model_copy(update={"llm_max_concurrency": 1, "llm_max_tokens": MAX_TOKENS})
    if PRIOR_ESTIMATED_CNY + RESERVE_PER_REQUEST * MAX_REQUESTS > MAX_CNY:
        raise RegressionStop("Worst-case cost exceeds approval")
    output.mkdir(parents=True, exist_ok=False)
    if claim:
        # Kept after completion/failure: a new run needs fresh authorization.
        with (output.parent / (SESSION + ".claim")).open("x", encoding="utf-8") as handle:
            handle.write(str(output))
            handle.flush()
            os.fsync(handle.fileno())
    report = dict(session=SESSION, model=settings.llm_model, generator=LIGHTWEIGHT_GENERATOR_VERSION,
        grader=LIGHTWEIGHT_GRADER_VERSION, reasoning_effort=settings.llm_reasoning_effort,
        prior_http_requests=PRIOR_REQUESTS, cumulative_http_cap=50,
        prior_estimated_peak_cny=PRIOR_ESTIMATED_CNY,
        max_requests=MAX_REQUESTS, additional_http_cap=MAX_REQUESTS,
        max_tokens=MAX_TOKENS, max_body_bytes=MAX_BODY_BYTES,
        max_cny=MAX_CNY, concurrency=1, price_source=PRICE_SOURCE, price_checked="2026-09-28",
        worst_case_cny=PRIOR_ESTIMATED_CNY + RESERVE_PER_REQUEST * MAX_REQUESTS,
        http_requests=0, reserved_peak_cny=0.0, estimated_peak_cny=0.0,
        cumulative_estimated_peak_cny=PRIOR_ESTIMATED_CNY,
        requests=[], generation=[], grading=[],
        generation_semantic_review="pending", started_at=time.time())

    def save():
        temp = output / "report.tmp"
        with temp.open("w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        temp.replace(output / "report.json")

    save()
    provider = provider_factory(settings, RequestGuard(report, save))
    try:
        for case in CASES:
            report["active_case"] = "generate:" + case["name"]
            started = time.monotonic()
            generated, _ = await provider.generate_lightweight(title=case["title"], statement=case["statement"],
                language="C++", source_code=case["source"], second_question_kind=case["kind"])
            report["generation"].append(dict(name=case["name"], kind=case["kind"],
                seconds=time.monotonic()-started, questions=generated.model_dump(mode="json")))
            save()
            print(f"generation={case['name']} structure=ok", flush=True)
        for name, case_index, question, question_en, answer, score, dispute in fixed_checks():
            case = CASES[case_index]
            report["active_case"] = "grade:" + name
            started = time.monotonic()
            result, _ = await provider.grade_lightweight(title=case["title"], statement=case["statement"],
                language="C++", source_code=case["source"], question_payload=[dict(question_index=1,
                    question=question, question_en=question_en, student_answer=answer)])
            grade = result.grades[0]
            effective_review = grade.student_dispute or grade.needs_teacher_review
            passed = (len(result.grades) == 1 and grade.score == score
                and grade.student_dispute == dispute and effective_review == dispute)
            report["grading"].append(dict(name=name, seconds=time.monotonic()-started,
                expected_score=score, expected_dispute=dispute, passed=passed,
                result=result.model_dump(mode="json")))
            save()
            print(f"grading={name} passed={passed}", flush=True)
        report["grading_passed"] = all(item["passed"] for item in report["grading"])
        report["completed"] = True
    except Exception as exc:
        report["error_type"] = type(exc).__name__
        if isinstance(exc, RegressionStop): report["stop_reason"] = str(exc)
        raise
    finally:
        report["elapsed_seconds"] = time.time() - report["started_at"]
        save()
        await provider.close()
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--confirm-real-cost", action="store_true")
    args = parser.parse_args()
    if not args.confirm_real_cost: parser.error("Explicit fixed-scope cost approval required")
    try:
        report = asyncio.run(run(args.output))
        print(f"regression=completed requests={report['http_requests']} grading_passed={report['grading_passed']} semantic_review=pending", flush=True)
        if not report["grading_passed"]: raise SystemExit(2)
    except Exception as exc:
        print(f"regression=stopped reason={type(exc).__name__}", flush=True)
        raise SystemExit(1)


if __name__ == "__main__": main()
