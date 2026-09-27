import { useEffect, useState } from "react";
import { api } from "../api";
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
  max_score?: number; percent?: number; submitted_at?: string;
  submission_source?: string; questions?: ResultQuestion[];
}

export function StudentResults({ quizId, onLogout }: { quizId: string; onLogout: () => void }) {
  const [result, setResult] = useState<Result | null>(null);
  const [error, setError] = useState("");
  const [appealFor, setAppealFor] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  async function refresh() {
    try {
      setResult(await api<Result>(`/api/quiz/${quizId}/my-result`));
      setError("");
    } catch (caught) { setError((caught as Error).message); }
  }
  useEffect(() => { void refresh(); }, [quizId]);

  async function submitAppeal(questionId: string) {
    setBusy(true);
    try {
      await api(`/api/quiz/${quizId}/questions/${questionId}/appeal`, {
        method: "POST", body: JSON.stringify({ reason, idempotency_key: crypto.randomUUID() }),
      });
      setAppealFor(null);
      setReason("");
      await refresh();
    } catch (caught) { setError((caught as Error).message); }
    finally { setBusy(false); }
  }
  return <main className="center-page result-page"><section className="card result-card">
    <div className="result-header"><h1>测评结果</h1><div>
      <button type="button" onClick={() => void refresh()}>刷新</button>
      <button type="button" onClick={onLogout}>退出登录</button>
    </div></div>
    {error && <div className="error" role="alert">{error}</div>}
    {!result ? <p>正在读取结果…</p> : !result.published ?
      <p>已交卷，成绩尚未公布。请在教师公布后使用同一链接查看。</p> :
      result.participated === false ? <p>本次测评未参加，没有成绩。</p> :
      <>
        <p className="result-total"><strong>{result.score} / {result.max_score}</strong> · {result.percent?.toFixed(1)}%</p>
        {result.submitted_at && <p>完成时间：{new Date(result.submitted_at).toLocaleString("zh-CN")} ·
          {result.submission_source === "timeout" ? "超时自动交卷" : "手动交卷"}</p>}
        <div className="result-questions">
          {result.questions?.map(q => <article key={q.id} className="result-question">
            <h2>问题 {q.index} · {q.score} / 2 分</h2>
            <ProblemStatement text={q.question} />
            {q.choices && <ol className="result-options">{q.choices.map(choice =>
              <li key={choice.id}>{choice.id}. <ProblemStatement text={choice.text} /></li>)}</ol>}
            <p>你的答案：{q.choice_id || q.answer_text || "未作答"}</p>
            <p>评分反馈：{q.reason || "暂无"}</p>
            {q.reference_answer && <p>参考答案：{q.reference_answer}</p>}
            {q.correct_choice_id && <p>正确选项：{q.correct_choice_id}</p>}
            {q.appeals.map(a => <p key={a.id}>申请复核：{a.state === "pending" ? "处理中" : "已处理"} ·
              {a.reason}{a.resolution && ` · 处理说明：${a.resolution}`}</p>)}
            <button type="button" disabled={q.appeals.some(a => a.state === "pending" || a.question_score_version === q.score_version)}
              onClick={() => { setAppealFor(q.id); setReason(""); }}>申请复核</button>
            {appealFor === q.id && <div className="appeal-form">
              <label>请说明你认为需要重新检查的地方
                <textarea value={reason} maxLength={1000} onChange={event => setReason(event.target.value)} />
              </label>
              <button type="button" disabled={busy || !reason.trim()} onClick={() => void submitAppeal(q.id)}>提交申请</button>
              <button type="button" onClick={() => setAppealFor(null)}>取消</button>
            </div>}
          </article>)}
        </div>
      </>}
  </section></main>;
}
