import { useEffect, useRef, useState } from "react";
import { ArrowUpRight, CheckCheck, MessageSquareText, X } from "lucide-react";
import { ProblemStatement } from "./ProblemStatement";

export interface AppealItem {
  id: string; attempt_id: string; question_id: string; state: string;
  reason: string; score_snapshot: number; question_snapshot: string;
  answer_snapshot: string; resolution: string | null; created_at?: string;
  feedback_snapshot?: string | null;
}

export function AppealPanel({ appeals, studentFor, busy, onReview, onViewAttempt }: {
  appeals: AppealItem[]; studentFor: (attemptId: string) => string;
  busy: boolean; onReview: (appeal: AppealItem) => void; onViewAttempt: (id: string) => void;
}) {
  const [filter, setFilter] = useState<"pending" | "resolved" | "all">("pending");
  const pending = appeals.filter(appeal => appeal.state === "pending");
  const resolvedCount = appeals.length - pending.length;
  const visible = appeals.filter(appeal => filter === "all" || (filter === "pending" ? appeal.state === "pending" : appeal.state !== "pending"));
  return <section className="card appeals-panel" aria-labelledby="appeals-heading">
    <div className="appeals-heading">
      <div className="section-title"><span className="section-icon"><MessageSquareText size={20} /></span>
        <div><h2 id="appeals-heading">学生疑问 <span className={`count-badge ${pending.length ? "has-pending" : ""}`}>{pending.length}</span></h2>
          <p>回复学生疑问，或切换筛选回看历史疑问与教师回复。</p></div></div>
      <span className="appeals-summary">已回复 {resolvedCount} 条</span>
    </div>
    <div className="filter-tabs" role="group" aria-label="疑问状态筛选">
      {([['pending', '待回复', pending.length], ['resolved', '已回复', resolvedCount], ['all', '全部', appeals.length]] as const).map(([value, label, count]) =>
        <button type="button" key={value} className={filter === value ? "active" : ""} aria-pressed={filter === value}
          onClick={() => setFilter(value)}>{label}（{count}）</button>)}
    </div>
    {visible.length ? <div className="appeals-grid">{visible.map(appeal => <article className="appeal-item" key={appeal.id}>
      <div className="appeal-item-heading"><strong className="mono">{studentFor(appeal.attempt_id)}</strong><span className={`status-pill ${appeal.state === "pending" ? "appeal-pending" : "status-finished"}`}>{appeal.state === "pending" ? "待回复" : "已回复"}</span></div>
      <div className="appeal-meta"><span>申请时得分 <strong>{appeal.score_snapshot} / 2</strong></span>{appeal.created_at && <time dateTime={appeal.created_at}>{new Intl.DateTimeFormat("zh-CN", {month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false}).format(new Date(appeal.created_at))}</time>}</div>
      <p className="appeal-reason">{appeal.reason}</p>
      <details className="appeal-context"><summary>查看题目与学生答案</summary><div><span className="field-caption">申请时题目</span><ProblemStatement text={appeal.question_snapshot} /><span className="field-caption">申请时学生答案</span><p className="preserve-text">{appeal.answer_snapshot || "未作答"}</p>{appeal.feedback_snapshot && <><span className="field-caption">申请时作答反馈</span><p className="preserve-text">{appeal.feedback_snapshot}</p></>}</div></details>
      {appeal.state !== "pending" && <div><span className="field-caption">教师处理说明</span><p className="preserve-text">{appeal.resolution || "暂无处理说明"}</p></div>}
      <div className="appeal-item-actions"><button className="link" disabled={busy} onClick={() => onViewAttempt(appeal.attempt_id)}>完整作答<ArrowUpRight size={14} /></button>{appeal.state === "pending" && <button className="secondary" disabled={busy} onClick={() => onReview(appeal)}>回复疑问</button>}</div>
    </article>)}</div> : <div className="appeals-empty"><span><CheckCheck size={23} /></span><div><strong>{filter === "pending" ? "暂无待回复疑问" : filter === "resolved" ? "暂无已回复疑问" : "暂无疑问记录"}</strong><p>{filter === "pending" && resolvedCount ? "可切换到“已回复”查看历史疑问与回复。" : "当前筛选下没有疑问。"}</p></div></div>}
  </section>;
}

export function AppealReviewDialog({ appeal, student, resolution, score, busy, error, onResolution, onScore, onCancel, onSubmit }: {
  appeal: AppealItem; student: string; resolution: string; score: string; busy: boolean; error: string;
  onResolution: (value: string) => void; onScore: (value: string) => void;
  onCancel: () => void; onSubmit: () => void;
}) {
  const dialog = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    dialog.current?.querySelector<HTMLTextAreaElement>("textarea")?.focus();
    return () => previous?.focus();
  }, []);
  return <div className="question-edit-overlay" onMouseDown={event => { if (event.target === event.currentTarget && !busy) onCancel(); }}>
    <div className="card appeal-review-dialog" role="dialog" aria-modal="true" aria-labelledby="appeal-title" ref={dialog}
      onKeyDown={event => {
        if (event.key === "Escape" && !busy) { event.preventDefault(); onCancel(); }
        if (event.key === "Tab") {
          const controls = dialog.current?.querySelectorAll<HTMLElement>("button:not(:disabled), textarea, select, summary");
          if (!controls?.length) return;
          const first = controls[0], last = controls[controls.length - 1];
          if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
          else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
        }
      }}>
      <div className="appeal-dialog-heading"><div><h2 id="appeal-title">回复学生疑问</h2><p><span className="mono">{student}</span><span>申请时得分 {appeal.score_snapshot} / 2</span></p></div><button className="icon-button" aria-label="关闭疑问回复" disabled={busy} onClick={onCancel}><X size={20} /></button></div>
      <div className="appeal-dialog-body"><div className="appeal-review-context"><span className="field-caption">申请理由</span><p className="preserve-text">{appeal.reason}</p><details className="appeal-context"><summary>查看题目与学生答案</summary><div><ProblemStatement text={appeal.question_snapshot} /><span className="field-caption">学生答案</span><p className="preserve-text">{appeal.answer_snapshot || "未作答"}</p></div></details></div>
        <label>处理说明<textarea required value={resolution} maxLength={5000} placeholder="说明核对结果与评分依据，学生将看到这段回复。" onChange={event => onResolution(event.target.value)} /></label>
        <label>本题得分<select value={score} onChange={event => onScore(event.target.value)}><option value="">维持当前分数</option><option value="0">调整为 0 分</option><option value="1">调整为 1 分</option><option value="2">调整为 2 分</option></select></label>
        {error && <div className="error" role="alert">{error}</div>}
      </div>
      <div className="appeal-dialog-actions"><button type="button" className="secondary" disabled={busy} onClick={onCancel}>取消</button><button disabled={busy || !resolution.trim()} onClick={onSubmit}>{busy ? "正在处理…" : "确认处理并回复"}</button></div>
    </div>
  </div>;
}
