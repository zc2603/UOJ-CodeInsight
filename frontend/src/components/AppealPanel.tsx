import { useEffect, useRef } from "react";
import { ArrowUpRight, CheckCheck, MessageSquareText, X } from "lucide-react";
import { ProblemStatement } from "./ProblemStatement";

export interface AppealItem {
  id: string; attempt_id: string; question_id: string; state: string;
  reason: string; score_snapshot: number; question_snapshot: string;
  answer_snapshot: string; resolution: string | null; created_at?: string;
}

export function AppealPanel({ appeals, studentFor, busy, onReview, onViewAttempt }: {
  appeals: AppealItem[]; studentFor: (attemptId: string) => string;
  busy: boolean; onReview: (appeal: AppealItem) => void; onViewAttempt: (id: string) => void;
}) {
  const pending = appeals.filter(appeal => appeal.state === "pending");
  return <section className={`card appeals-panel ${pending.length ? "" : "appeals-panel-empty"}`} aria-labelledby="appeals-heading">
    <div className="appeals-heading">
      <div className="section-title"><span className="section-icon"><MessageSquareText size={20} /></span>
        <div><h2 id="appeals-heading">学生申诉 <span className={`count-badge ${pending.length ? "has-pending" : ""}`}>{pending.length}</span></h2>
          <p>{pending.length ? "查看学生的疑问，核对作答后给出处理结果。" : "学生对评分有疑问时，申请会显示在这里。"}</p></div></div>
      <span className="appeals-summary">已处理 {appeals.length - pending.length} 条</span>
    </div>
    {pending.length ? <div className="appeals-grid">{pending.map(appeal => <article className="appeal-item" key={appeal.id}>
      <div className="appeal-item-heading"><strong className="mono">{studentFor(appeal.attempt_id)}</strong><span className="status-pill appeal-pending">待处理</span></div>
      <div className="appeal-meta"><span>申请时得分 <strong>{appeal.score_snapshot} / 2</strong></span>{appeal.created_at && <time dateTime={appeal.created_at}>{new Intl.DateTimeFormat("zh-CN", {month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false}).format(new Date(appeal.created_at))}</time>}</div>
      <p className="appeal-reason">{appeal.reason}</p>
      <details className="appeal-context"><summary>查看题目与学生答案</summary><div><span className="field-caption">题目</span><ProblemStatement text={appeal.question_snapshot} /><span className="field-caption">学生答案</span><p className="preserve-text">{appeal.answer_snapshot || "未作答"}</p></div></details>
      <div className="appeal-item-actions"><button className="link" disabled={busy} onClick={() => onViewAttempt(appeal.attempt_id)}>完整作答<ArrowUpRight size={14} /></button><button className="secondary" disabled={busy} onClick={() => onReview(appeal)}>处理申诉</button></div>
    </article>)}</div> : <div className="appeals-empty"><span><CheckCheck size={23} /></span><div><strong>暂无待处理申诉</strong><p>目前没有需要回复的申请。</p></div></div>}
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
      <div className="appeal-dialog-heading"><div><h2 id="appeal-title">处理学生申诉</h2><p><span className="mono">{student}</span><span>申请时得分 {appeal.score_snapshot} / 2</span></p></div><button className="icon-button" aria-label="关闭申诉处理" disabled={busy} onClick={onCancel}><X size={20} /></button></div>
      <div className="appeal-dialog-body"><div className="appeal-review-context"><span className="field-caption">申请理由</span><p className="preserve-text">{appeal.reason}</p><details className="appeal-context"><summary>查看题目与学生答案</summary><div><ProblemStatement text={appeal.question_snapshot} /><span className="field-caption">学生答案</span><p className="preserve-text">{appeal.answer_snapshot || "未作答"}</p></div></details></div>
        <label>处理说明<textarea required value={resolution} maxLength={5000} placeholder="说明核对结果与评分依据，学生将看到这段回复。" onChange={event => onResolution(event.target.value)} /></label>
        <label>本题得分<select value={score} onChange={event => onScore(event.target.value)}><option value="">维持当前分数</option><option value="0">调整为 0 分</option><option value="1">调整为 1 分</option><option value="2">调整为 2 分</option></select></label>
        {error && <div className="error" role="alert">{error}</div>}
      </div>
      <div className="appeal-dialog-actions"><button type="button" className="secondary" disabled={busy} onClick={onCancel}>取消</button><button disabled={busy || !resolution.trim()} onClick={onSubmit}>{busy ? "正在处理…" : "确认处理并回复"}</button></div>
    </div>
  </div>;
}
