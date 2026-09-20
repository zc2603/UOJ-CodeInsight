import { useEffect, useState } from "react";
import { api } from "../api";
import type { QualityReview } from "../types";

export function QualityAudit({ review }: { review?: QualityReview }) {
  const [value, setValue] = useState(review);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => { setValue(review); }, [review]);
  useEffect(() => {
    if (!value?.job_id || !["queued", "running"].includes(value.state)) return;
    let active = true;
    const timer = window.setInterval(() => {
      void api<QualityReview>(`/api/admin/quality-audits/${value.job_id}/${value.index}`)
        .then(next => { if (active) { setValue(next); setError(""); } })
        .catch(() => { if (active) setError("审核状态刷新失败，请稍后重新打开详情"); });
    }, 10000);
    return () => { active = false; window.clearInterval(timer); };
  }, [value?.job_id, value?.index, value?.state]);
  if (!value) return null;
  const labels: Record<string, string> = { not_requested: "未审核", queued: "待审核", running: "审核中", failed: "审核失败", pass: "通过", fail: "明确不通过", uncertain: "无法确认" };
  async function act(action: "acknowledge" | "request") {
    if (!value?.job_id) return;
    if (action === "request" && !window.confirm("将审核这道原题的两个问题，产生一次模型请求费用，不修改题目或成绩。确认继续？")) return;
    setBusy(true); setError("");
    try {
      await api(`/api/admin/quality-audits/${value.job_id}/${action === "acknowledge" ? `${value.index}/acknowledge` : "request"}`, { method: "POST" });
      setValue(await api<QualityReview>(`/api/admin/quality-audits/${value.job_id}/${value.index}`));
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  return <section className="quality-review" aria-label="题目质量审核">
    <strong>题目质量：{labels[value.verdict || value.state] || value.state}</strong>
    {value.confidence != null && <span>判定置信度 {value.confidence.toFixed(2)}</span>}
    {value.attention && <span className="status-pill quality-attention">需关注{value.verdict === "pass" ? "：通过但把握不足" : ""}</span>}
    {value.acknowledged && <span>已查看（不影响成绩）</span>}
    {value.reason && <p>{value.reason}</p>}
    {value.attention && <button className="link" disabled={busy} onClick={() => void act("acknowledge")}>标记已查看</button>}
    {value.job_id && ["not_requested", "failed"].includes(value.state) && <button className="link" disabled={busy} onClick={() => void act("request")}>{value.state === "failed" ? "重试审核" : "申请审核"}</button>}
    {error && <p role="alert">{error}</p>}
  </section>;
}
