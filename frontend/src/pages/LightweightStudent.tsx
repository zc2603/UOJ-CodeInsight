import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { ProblemStatement } from "../components/ProblemStatement";
import { CodeBlock } from "../components/CodeBlock";
import type { LightweightDraft, StudentQuestion } from "../types";

type DraftMap = Record<string, LightweightDraft>;
type SaveReply = { saved: boolean; draft: LightweightDraft };

function editable(d: LightweightDraft) {
  return { answer_text: d.answer_text, choice_id: d.choice_id, revisit: d.revisit,
    dispute: d.dispute, dispute_reason: d.dispute_reason };
}

export function LightweightStudent({ quizId, initial, onDone }: {
  quizId: string; initial: StudentQuestion; onDone: (timedOut: boolean) => void;
}) {
  const [questions, setQuestions] = useState(initial.questions);
  const [drafts, setDrafts] = useState<DraftMap>(() => Object.fromEntries(initial.questions.map(q => [q.id, q.draft])));
  const [index, setIndex] = useState(0);
  const [now, setNow] = useState(Date.now());
  const [serverOffset, setServerOffset] = useState(initial.server_time
    ? new Date(initial.server_time).getTime() - Date.now() : 0);
  const [saveState, setSaveState] = useState<Record<string, string>>({});
  const [conflict, setConflict] = useState<StudentQuestion | null>(null);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [showEnglish, setShowEnglish] = useState(false);
  const latest = useRef(drafts);
  latest.current = drafts;
  const revisions = useRef<Record<string, number>>(Object.fromEntries(initial.questions.map(q => [q.id, q.draft.revision])));
  const saved = useRef<Record<string, string>>(Object.fromEntries(initial.questions.map(q => [q.id, JSON.stringify(editable(q.draft))])));
  const inFlight = useRef<Partial<Record<string, Promise<void>>>>({});
  const submitKey = useRef(crypto.randomUUID());
  const [expired, setExpired] = useState(false);
  const current = questions[index];
  const seconds = initial.deadline_at
    ? Math.max(0, Math.ceil((new Date(initial.deadline_at).getTime() - now - serverOffset) / 1000)) : 0;
  const countAnswered = useMemo(() => questions.filter(q => {
    const d = drafts[q.id];
    return q.response_format === "single_choice" ? !!d?.choice_id : !!d?.answer_text.trim();
  }).length, [questions, drafts]);

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    if (seconds !== 0 || expired) return;
    setExpired(true);
    setMessage("时间已到，正在核对服务器自动交卷状态。");
    let cancelled = false;
    async function verify() {
      try {
        const data = await api<StudentQuestion>("/api/attempt/current");
        if (cancelled) return;
        if (["EXPIRED", "GRADING", "GRADING_ERROR", "FINISHED"].includes(data.status)) {
          onDone(true);
        } else {
          setMessage("服务器仍在收取已保存草稿，请稍后刷新核对。");
        }
      } catch {
        if (!cancelled) setMessage("网络中断，尚未确认交卷；服务器会按截止前保存的草稿收取。");
      }
    }
    void verify();
    return () => { cancelled = true; };
  }, [seconds, expired, onDone]);

  function update(id: string, change: Partial<LightweightDraft>) {
    setDrafts(previous => ({ ...previous, [id]: { ...previous[id], ...change } }));
    setSaveState(previous => ({ ...previous, [id]: "未保存" }));
  }

  async function refreshConflict() {
    try {
      const data = await api<StudentQuestion>("/api/attempt/current");
      if (data.assessment_version !== "lightweight_v1") return;
      setConflict(data);
      setMessage("草稿已在其他页面更新。请核对后选择保留本页或载入服务器版本。");
    } catch (error) { setMessage((error as Error).message); }
  }

  function save(id: string): Promise<void> {
    if (inFlight.current[id]) return inFlight.current[id];
    const question = questions.find(q => q.id === id);
    if (!question || seconds === 0 || busy || conflict) return Promise.resolve();
    const draft = latest.current[id];
    const serial = JSON.stringify(editable(draft));
    if (serial === saved.current[id]) return Promise.resolve();
    setSaveState(previous => ({ ...previous, [id]: "保存中" }));
    const pending = (async () => {
      let success = false;
      try {
        const response = await api<SaveReply>(`/api/attempt/${initial.attempt_id}/draft`, {
          method: "PUT", body: JSON.stringify({ question_id: id, expected_revision: revisions.current[id],
            ...editable(draft) }),
        });
        revisions.current[id] = response.draft.revision;
        saved.current[id] = serial;
        success = true;
        setSaveState(previous => ({ ...previous, [id]: "已保存" }));
      } catch (error) {
        setSaveState(previous => ({ ...previous, [id]: "保存失败，请重试" }));
        if ((error as { status?: number }).status === 409) void refreshConflict();
        else setMessage((error as Error).message);
      } finally {
        delete inFlight.current[id];
        if (success && JSON.stringify(editable(latest.current[id])) !== saved.current[id] && !conflict) {
          window.setTimeout(() => void save(id), 50);
        }
      }
    })();
    inFlight.current[id] = pending;
    return pending;
  }

  useEffect(() => {
    if (!current || busy || conflict || seconds === 0) return;
    const timer = window.setTimeout(() => void save(current.id), 450);
    return () => window.clearTimeout(timer);
  }, [drafts, index, busy, conflict, seconds === 0]);

  function navigate(next: number) {
    void save(current.id);
    setIndex(next);
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function resolveConflict(keepLocal: boolean) {
    if (!conflict) return;
    const server = Object.fromEntries(conflict.questions.map(q => [q.id, q.draft]));
    revisions.current = Object.fromEntries(conflict.questions.map(q => [q.id, q.draft.revision]));
    saved.current = Object.fromEntries(conflict.questions.map(q => [q.id, JSON.stringify(editable(q.draft))]));
    if (!keepLocal) setDrafts(server);
    setQuestions(conflict.questions);
    setServerOffset(conflict.server_time ? new Date(conflict.server_time).getTime() - Date.now() : 0);
    setConflict(null);
    setMessage(keepLocal ? "已保留本页输入，将用最新版本重新保存。" : "已载入服务器草稿。");
  }

  async function submit() {
    const unanswered = questions.length - countAnswered;
    if (!window.confirm(`已答 ${countAnswered} 题，未答 ${unanswered} 题。空题计 0 分，确定交卷吗？`)) return;
    setBusy(true);
    setMessage("");
    try {
      await Promise.all(Object.values(inFlight.current));
      const items = questions.map(q => ({ question_id: q.id, expected_revision: revisions.current[q.id],
        ...editable(latest.current[q.id]) }));
      const response = await api<{ source: string }>(`/api/attempt/${initial.attempt_id}/submit`, {
        method: "POST", body: JSON.stringify({ attempt_id: initial.attempt_id,
          idempotency_key: submitKey.current, drafts: items }),
      });
      onDone(response.source === "timeout");
    } catch (error) {
      if ((error as { status?: number }).status === 409) await refreshConflict();
      else if ((error as { status?: number }).status === 410) {
        setMessage("作答时间已结束，正在等待服务器收取已保存草稿。");
        setExpired(false);
      } else setMessage((error as Error).message);
    } finally { setBusy(false); }
  }

  if (!current) return <main className="center-page"><p>本次测评暂无可作答问题。</p></main>;
  const draft = drafts[current.id];
  return <main className="quiz-shell lightweight-quiz">
    <header className="quiz-header">
      <div className="quiz-title"><strong>代码理解测评</strong><span>{current.problem_title}</span></div>
      <div className="quiz-meta"><span>已答 {countAnswered} / {questions.length}</span>
        <div className={`timer ${seconds <= 60 ? "danger" : seconds <= 300 ? "warning" : ""}`}>
          <small>整份剩余时间</small>{String(Math.floor(seconds / 60)).padStart(2, "0")}:{String(seconds % 60).padStart(2, "0")}
        </div></div>
    </header>
    <nav className="lightweight-nav" aria-label="问题导航">
      {questions.map((q, i) => <button key={q.id} type="button" className={i === index ? "active" : ""}
        onClick={() => navigate(i)} aria-current={i === index ? "step" : undefined}>
        原题 {q.problem_id} · 问题 {q.index}
        <small>{q.draft.revisit || drafts[q.id].revisit ? "稍后再看" :
          (q.response_format === "single_choice" ? drafts[q.id].choice_id : drafts[q.id].answer_text.trim()) ? "已作答" : "未作答"}</small>
      </button>)}
    </nav>
    <section className="material-panel">
      <article className="problem-panel"><div className="material-title">原题描述</div>
        <ProblemStatement className="problem-statement" text={current.problem_statement} /></article>
      <section className="code-panel"><div className="panel-title">本人提交代码 <span>{current.language}</span></div>
        <CodeBlock code={current.source_code} /></section>
    </section>
    <section className="answer-panel">
      <div className="question-heading"><span className="question-type">{current.type}</span>
        <span className="question-sequence">问题 {index + 1} / {questions.length}</span>
        <button type="button" className="text-button language-toggle" onClick={() => setShowEnglish(!showEnglish)}>
          {showEnglish ? "Hide English" : "Show English"}</button></div>
      <ProblemStatement className="generated-question" text={showEnglish ? current.question_en : current.question} />
      {current.response_format === "short_answer" ?
        <label className="answer-label">你的回答
          <textarea value={draft.answer_text} disabled={seconds === 0 || busy} maxLength={5000}
            onChange={event => update(current.id, { answer_text: event.target.value })} /></label> :
        <fieldset className="lightweight-choices" disabled={seconds === 0 || busy}>
          <legend>选择一个答案</legend>
          {current.choices?.map(option => <label key={option.id} className={draft.choice_id === option.id ? "selected" : ""}>
            <input type="radio" name={current.id} checked={draft.choice_id === option.id}
              onChange={() => update(current.id, { choice_id: option.id })} />
            <strong>{option.id}</strong><ProblemStatement text={showEnglish ? option.text_en : option.text} />
          </label>)}
          <button type="button" className="text-button" onClick={() => update(current.id, { choice_id: null })}>清空选择</button>
        </fieldset>}
      <div className="lightweight-flags">
        <label><input type="checkbox" checked={draft.revisit}
          onChange={event => update(current.id, { revisit: event.target.checked })} /> 稍后再看</label>
        <label><input type="checkbox" checked={draft.dispute}
          onChange={event => update(current.id, { dispute: event.target.checked,
            dispute_reason: event.target.checked ? draft.dispute_reason : null })} /> 题目有疑问</label>
        {draft.dispute && <textarea aria-label="题目疑问说明" placeholder="可填写疑问说明；不填写也会交教师复核"
          value={draft.dispute_reason || ""} maxLength={1000}
          onChange={event => update(current.id, { dispute_reason: event.target.value })} />}
      </div>
      {message && <div className="error" role="alert">{message}</div>}
      {conflict && <div className="lightweight-conflict" role="alert">
        <p>服务器上的草稿与本页不同。请选择保留本页输入重新保存，或载入服务器版本。</p>
        <button type="button" onClick={() => resolveConflict(true)}>保留本页输入</button>
        <button type="button" onClick={() => resolveConflict(false)}>载入服务器草稿</button>
      </div>}
      <div className="form-footer"><span aria-live="polite">{saveState[current.id] || "已保存"}</span>
        <div className="lightweight-actions">
          <button type="button" disabled={index === 0} onClick={() => navigate(index - 1)}>上一题</button>
          {index < questions.length - 1
            ? <button type="button" onClick={() => navigate(index + 1)}>下一题</button>
            : <button type="button" disabled={busy || seconds === 0 || !!conflict} onClick={() => void submit()}>
              检查并交卷</button>}
        </div>
      </div>
    </section>
  </main>;
}
