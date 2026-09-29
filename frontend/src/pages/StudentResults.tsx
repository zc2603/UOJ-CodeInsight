import { useEffect, useState } from "react";
import { BookOpenCheck, CheckCircle2, Clock3, FileCheck2, LoaderCircle, LogOut, MessageSquareText, RefreshCw } from "lucide-react";
import { api } from "../api";
import { createRequestId } from "../requestId";
import { ProblemStatement } from "../components/ProblemStatement";

interface Appeal { id: string; state: string; reason: string; resolution: string | null; question_score_version: number }
interface ResultQuestion {
  id: string; index: number; question: string; question_en: string;
  choices: { id: string; text: string; text_en: string }[] | null;
  answer_text: string; choice_id: string | null; score: number; reason: string | null;
  reference_answer?: string | null; correct_choice_id?: string | null; score_version: number; appeals: Appeal[];
}
interface Result {
  published: boolean; participated?: boolean; message?: string; score?: number;
  max_score?: number; submitted_at?: string;
  submission_source?: string; questions?: ResultQuestion[];
}

function scoreGrade(score: number | undefined) {
  if (score === undefined || !Number.isFinite(score) || score < 0) return "—";
  if (score >= 8) return "A+";
  if (score >= 6) return "A";
  if (score >= 4) return "B+";
  if (score >= 2) return "B";
  if (score >= 1) return "C";
  return "D";
}

export function StudentResults({ quizId, onLogout }: { quizId: string; onLogout: () => void }) {
  const [result, setResult] = useState<Result | null>(null);
  const [error, setError] = useState("");
  const [appealFor, setAppealFor] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [updatedAt, setUpdatedAt] = useState("");
  async function refresh() {
    setRefreshing(true);
    try {
      setResult(await api<Result>(`/api/quiz/${quizId}/my-result`));
      setUpdatedAt(new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false }));
      setError("");
    } catch (caught) { setError((caught as Error).message); }
    finally { setRefreshing(false); }
  }
  useEffect(() => { void refresh(); }, [quizId]);

  async function submitAppeal(questionId: string) {
    setBusy(true);
    try {
      await api(`/api/quiz/${quizId}/questions/${questionId}/appeal`, {
        method: "POST", body: JSON.stringify({ reason, idempotency_key: createRequestId() }),
      });
      setAppealFor(null);
      setReason("");
      await refresh();
    } catch (caught) { setError((caught as Error).message); }
    finally { setBusy(false); }
  }
  const hasScore = result?.published && result.participated !== false;
  return <main className={`result-page ${hasScore ? "result-page-published" : "result-page-status"}`}>
    <div className="student-page-bar">
      <div className="student-page-brand"><span className="brand-mark"><BookOpenCheck size={22} /></span>
        <span><strong>代码理解测评</strong><small>UOJ 课程辅助系统</small></span></div>
      <button type="button" className="secondary" onClick={onLogout}><LogOut size={16} />退出登录</button>
    </div>
    <section className="student-results-content" aria-labelledby="student-result-title">
    <header className="result-header"><div><span className="app-kicker">作答记录与反馈</span><h1 id="student-result-title">测评结果</h1></div>
      <button type="button" className="secondary" disabled={refreshing} onClick={() => void refresh()}>
        <RefreshCw size={16} className={refreshing ? "spin" : ""} />{refreshing ? "正在刷新" : "刷新状态"}</button>
    </header>
    {error && <div className="error" role="alert">{error}</div>}
    {!result ? <div className="card result-state" role="status"><span className="result-state-icon">
      {refreshing ? <LoaderCircle size={30} className="spin" /> : <RefreshCw size={30} />}</span>
      <h2>{error ? "暂时无法读取结果" : "正在读取测评结果"}</h2><p>{error ? "请检查网络连接，然后点击刷新状态重试。" : "正在核对本次测评的成绩状态，请稍候。"}</p></div> : !result.published ?
      <div className="card result-state">
        <span className="status-pill status-finished"><CheckCircle2 size={14} />作答已提交</span>
        <span className="result-state-icon"><Clock3 size={32} /></span>
        <h2>成绩尚未公布</h2><p>你的作答已保存，请等待教师公布成绩。<br />公布后，使用同一测评链接即可查看得分与反馈。</p>
        <div className="result-state-steps"><span><CheckCircle2 size={16} />完成作答</span><i /><strong><Clock3 size={16} />等待公布</strong><i /><span><FileCheck2 size={16} />查看反馈</span></div>
        <div className="result-state-note">现在可以关闭页面，无需停留等待。</div>
      </div> :
      result.participated === false ? <div className="card result-state"><span className="result-state-icon"><FileCheck2 size={32} /></span>
        <h2>本次测评未参加</h2><p>没有可查看的作答与成绩记录。</p></div> :
      <>
        <section className="card student-score-summary" aria-label="成绩概览">
          <div className="student-score-main"><span className="status-pill status-finished"><CheckCircle2 size={14} />成绩已公布</span>
            <p className="result-total"><strong>{scoreGrade(result.score)}</strong></p><span className="muted">本次测评等级</span></div>
          <div className="student-score-meta"><div><span>本次测评总分</span><strong>{result.score}<small> / {result.max_score} 分</small></strong></div>
            <div><span>完成时间</span><strong className="completion-time">{result.submitted_at ? new Date(result.submitted_at).toLocaleString("zh-CN", { hour12: false }) : "—"}</strong>
              <small>{result.submission_source === "timeout" ? "超时自动交卷" : "手动交卷"}</small></div></div>
        </section>
        <div className="result-section-heading"><div><h2>逐题回顾</h2><p>核对你的作答，了解评分依据与参考答案。</p></div><span>{result.questions?.length ?? 0} 个问题</span></div>
        <div className="result-questions">
          {result.questions?.map(q => <article key={q.id} className="card result-question">
            <header className="result-question-heading"><h3><span className="question-number">{String(q.index).padStart(2, "0")}</span>问题 {q.index}
              <span className="question-format-label">{q.choices ? "单选题" : "简答题"}</span></h3><span className="question-score">{q.score} / 2 分</span></header>
            <div className="result-question-body"><ProblemStatement className="generated-question" text={q.question} />
            {q.choices && <ol className="result-options">{q.choices.map(choice =>
              <li key={choice.id} className={choice.id === q.correct_choice_id ? "correct" : choice.id === q.choice_id ? "chosen" : ""}>
                <strong>{choice.id}</strong><ProblemStatement text={choice.text} /><span className="option-annotation">
                  {choice.id === q.choice_id && "你的选择"}{choice.id === q.choice_id && choice.id === q.correct_choice_id && " · "}{choice.id === q.correct_choice_id && "正确选项"}</span></li>)}</ol>}
            <div className="result-feedback-grid"><section className="student-answer-box"><h4>你的答案</h4><p className="preserve-text">{q.choice_id || q.answer_text || "未作答"}</p></section>
              {(q.reference_answer || q.correct_choice_id) && <section className="reference-answer-box"><h4>参考答案</h4>
                {q.correct_choice_id && <p className="correct-choice">正确选项：{q.correct_choice_id}</p>}{q.reference_answer && <ProblemStatement text={q.reference_answer} />}</section>}</div>
            <section className="result-feedback"><h4><MessageSquareText size={16} />评分反馈</h4><p>{q.reason || "暂无评分说明"}</p></section>
            {q.appeals.map(a => <div key={a.id} className="student-appeal-record"><span className={`status-pill ${a.state === "pending" ? "appeal-pending" : "status-finished"}`}>复核申请 · {a.state === "pending" ? "处理中" : "已处理"}</span>
              <p className="preserve-text">{a.reason}</p>{a.resolution && <div><strong>教师回复</strong><p className="preserve-text">{a.resolution}</p></div>}</div>)}
            <div className="result-question-actions"><span>对本题评分有疑问？可向教师申请复核。</span>
              <button type="button" className="secondary" disabled={busy || q.appeals.some(a => a.state === "pending" || a.question_score_version === q.score_version)}
                aria-expanded={appealFor === q.id} onClick={() => { setAppealFor(q.id); setReason(""); }}>申请复核</button></div>
            {appealFor === q.id && <form className="appeal-form" onSubmit={event => { event.preventDefault(); if (!busy && reason.trim()) void submitAppeal(q.id); }}>
              <label>请说明你认为需要重新检查的地方
                <textarea autoFocus required disabled={busy} value={reason} maxLength={1000} placeholder="可以结合你的回答，说明希望教师核对的内容。" onChange={event => setReason(event.target.value)} />
              </label>
              <div className="appeal-form-actions"><small>{reason.length} / 1000</small><button type="button" className="secondary" disabled={busy} onClick={() => setAppealFor(null)}>取消</button>
                <button type="submit" disabled={busy || !reason.trim()}>{busy ? "正在提交…" : "提交申请"}</button></div>
            </form>}</div>
          </article>)}
        </div>
      </>}
      <footer className="result-page-footer"><span>仅展示你本人的作答与成绩</span><span role="status">{updatedAt && `最近更新 ${updatedAt}`}</span></footer>
    </section></main>;
}
