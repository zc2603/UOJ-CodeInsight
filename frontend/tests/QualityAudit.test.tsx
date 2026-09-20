import { renderToStaticMarkup } from "react-dom/server";
import { QualityAudit } from "../src/components/QualityAudit";
import type { QualityReview } from "../src/types";
const render = (overrides: Partial<QualityReview>) => renderToStaticMarkup(<QualityAudit review={{ state: "done", verdict: "pass", confidence: .9, attention: false, acknowledged: false, job_id: "test", index: 1, ...overrides }} />);
function check(ok: boolean, message: string) { if (!ok) throw new Error(message); }
check(render({}).includes("通过") && render({}).includes("重新审核"), "passed questions can be re-audited");
check(render({ confidence: .7, attention: true }).includes("通过但把握不足"), "low confidence");
check(render({ verdict: "fail", attention: true }).includes("标记已查看"), "acknowledge");
check(!render({ verdict: "fail", acknowledged: true }).includes("标记已查看"), "acknowledged");
check(render({ verdict: null, state: "failed", attention: true }).includes("重试审核"), "technical failure");
check(!render({ verdict: null, state: "queued" }).includes("标记已查看"), "pending is not a quality defect");
check(!render({ reason: '<script>alert(1)</script>' }).includes("<script>"), "model reason is escaped");
console.log("QualityAudit: 7 rendering checks passed");
