import { FormEvent, useEffect, useMemo, useState } from "react";
import {
  BookOpenCheck, CheckCircle2, Clock3, Languages, ListChecks, LoaderCircle,
  LockKeyhole, Monitor, UserRound,
} from "lucide-react";
import md5 from "blueimp-md5";
import { api } from "../api";
import { CodeBlock } from "../components/CodeBlock";
import { readableGeneratedQuestion } from "../generatedText";
import type { StudentQuestion } from "../types";

interface Props { quizId: string }

function remainingSeconds(deadline: string | null, now: number) {
  if (!deadline) return null;
  return Math.max(0, Math.ceil((new Date(deadline).getTime() - now) / 1000));
}

function formatRemaining(seconds: number | null) {
  if (seconds === null) return "--:--";
  return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
}

function questionTypeLabel(type: string | null | undefined) {
  if (type === "explanation") return "代码理解";
  if (type === "trace") return "执行追踪";
  if (type === "boundary_or_modification") return "边界与修改";
  return "当前问题";
}

export function StudentApp({ quizId }: Props) {
  const [studentNumber, setStudentNumber] = useState("");
  const [quizCode, setQuizCode] = useState("");
  const [uojPassword, setUojPassword] = useState("");
  const [passwordLogin, setPasswordLogin] = useState(false);
  const [passwordAvailable, setPasswordAvailable] = useState(false);
  const [passwordClientSalt, setPasswordClientSalt] = useState("");
  const [showEnglish, setShowEnglish] = useState(false);
  const [preGenerated, setPreGenerated] = useState(false);
  const [quizName, setQuizName] = useState("");
  const [screen, setScreen] = useState<"login" | "ready" | "quiz" | "done">("login");
  const [question, setQuestion] = useState<StudentQuestion | null>(null);
  const [answer, setAnswer] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [now, setNow] = useState(Date.now());

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    void api<{ uoj_password_enabled: boolean; uoj_password_client_salt: string | null }>(`/api/quiz/${quizId}/login-options`)
      .then(data => {
        setPasswordAvailable(data.uoj_password_enabled);
        setPasswordClientSalt(data.uoj_password_client_salt || "");
        setPasswordLogin(data.uoj_password_enabled);
      })
      .catch(() => undefined);
    return () => window.clearInterval(timer);
  }, [quizId]);

  const secondsLeft = useMemo(() => remainingSeconds(question?.deadline_at || null, now), [question, now]);
  const remaining = formatRemaining(secondsLeft);
  const timerTone = secondsLeft !== null && secondsLeft <= 60 ? "danger" : secondsLeft !== null && secondsLeft <= 300 ? "warning" : "";
  const progress = ((question?.question_index || 0) / (question?.question_count || 1)) * 100;
  const isLastQuestion = question?.question_index === question?.question_count;

  async function login(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setMessage("");
    try {
      const data = await api<{ quiz_name: string; pre_generated: boolean }>(`/api/quiz/${quizId}/login`, {
        method: "POST",
        body: JSON.stringify({
          student_number: studentNumber,
          ...(passwordLogin ? { uoj_password_hash: md5(uojPassword, passwordClientSalt) } : { quiz_code: quizCode }),
        }),
      });
      setQuizName(data.quiz_name);
      setPreGenerated(data.pre_generated);
      setScreen("ready");
    } catch (error) {
      setMessage((error as Error).message);
    } finally {
      setUojPassword("");
      setBusy(false);
    }
  }

  async function start() {
    setBusy(true);
    setMessage("");
    try {
      const data = await api<StudentQuestion>(`/api/quiz/${quizId}/start`, { method: "POST" });
      setQuestion(data);
      const alreadySubmitted = data.question_index === null || ["GRADING", "GRADING_ERROR", "FINISHED"].includes(data.status);
      setScreen(alreadySubmitted ? "done" : "quiz");
    } catch (error) {
      setMessage((error as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!question?.question_index || !answer.trim()) return;
    setBusy(true);
    setMessage("");
    try {
      const data = await api<StudentQuestion | { status: string; submitted: boolean }>("/api/attempt/current/answer", {
        method: "POST", body: JSON.stringify({ question_index: question.question_index, answer }),
      });
      setAnswer("");
      if ("submitted" in data) setScreen("done");
      else setQuestion(data);
    } catch (error) {
      setMessage((error as Error).message);
    } finally {
      setBusy(false);
    }
  }

  if (screen === "login") return (
    <main className="center-page auth-page">
      <div className="auth-brand">
        <span className="brand-mark"><BookOpenCheck size={24} /></span>
        <span><strong>UOJ 课程辅助系统</strong><small>代码理解测评平台</small></span>
      </div>
      <form className="card login-card" onSubmit={login}>
        <div className="role-label"><UserRound size={16} /> 学生测评端</div>
        <h1>进入代码理解测评</h1>
        <p className="muted">请使用本人的 UOJ 账号验证身份。</p>
        <label>UOJ 用户名<input autoComplete="username" value={studentNumber} onChange={event => setStudentNumber(event.target.value)} placeholder="请输入 UOJ 用户名" required /></label>
        {passwordLogin
          ? <label>UOJ 密码<input type="password" autoComplete="current-password" value={uojPassword} onChange={event => setUojPassword(event.target.value)} placeholder="请输入 UOJ 密码" required /></label>
          : <label>备用测评码<input className="code-input" value={quizCode} onChange={event => setQuizCode(event.target.value.toUpperCase())} placeholder="8 位代码" required /></label>}
        {passwordAvailable && <button type="button" className="link login-switch" onClick={() => setPasswordLogin(!passwordLogin)}>{passwordLogin ? "改用备用测评码" : "改用 UOJ 密码"}</button>}
        {message && <div className="error" role="alert">{message}</div>}
        <button disabled={busy}>{busy && <LoaderCircle className="spin" size={17} />}{busy ? "正在验证" : "验证身份并继续"}</button>
      </form>
      <div className="auth-footer">请在教师指定的时间和设备上完成测评</div>
    </main>
  );

  if (screen === "ready") return (
    <main className="center-page ready-page">
      <section className="card ready-card">
        <div className="ready-header"><span className="brand-mark"><BookOpenCheck size={22} /></span><div><div className="app-kicker">{quizName}</div><h1>开始前请确认</h1></div></div>
        <p className="ready-intro">本次测评包含你在本场 Contest 中的所有有效题目，每道题目有两个代码理解问题。</p>
        <div className="rule-grid">
          <article><span><LoaderCircle size={20} /></span><div><strong>{preGenerated ? "开始后计时" : "生成后计时"}</strong><p>{preGenerated ? "点击下方按钮开始测评，同时开始计时。" : "所有问题生成完成后才开始答题计时。"}</p></div></article>
          <article><span><Clock3 size={20} /></span><div><strong>按问题计时</strong><p>每个问题 3 分钟，系统自动计算总时长。</p></div></article>
          <article><span><ListChecks size={20} /></span><div><strong>逐题提交</strong><p>提交后进入下一问，不能返回修改。</p></div></article>
          <article><span><LockKeyhole size={20} /></span><div><strong>限定设备</strong><p>开始后仅允许当前设备继续作答。</p></div></article>
        </div>
        {message && <div className="error" role="alert">{message}</div>}
        <div className="ready-actions"><span><Monitor size={16} />请勿关闭或刷新答题页面</span><button onClick={start} disabled={busy}>{busy && <LoaderCircle className="spin" size={17} />}{busy ? (preGenerated ? "正在进入测评" : "正在生成问题") : "确认并开始测评"}</button></div>
      </section>
    </main>
  );

  if (screen === "done") return (
    <main className="center-page done-page"><section className="card ready-card done-card">
      <div className="success-symbol"><CheckCircle2 size={28} /></div>
      <div className="app-kicker">UOJ 代码理解测评</div>
      <h1>全部答案已提交</h1>
      <p>本次作答已经保存，可以关闭当前页面。</p>
    </section></main>
  );

  return <main className="quiz-shell">
    <header className="quiz-header">
      <div className="quiz-title"><span className="quiz-wordmark"><BookOpenCheck size={18} />代码理解测评</span><strong>题目 {question?.problem_id} · {question?.problem_title}</strong></div>
      <div className="quiz-meta"><span>总进度 <strong>{question?.question_index} / {question?.question_count}</strong></span><span>本题 <strong>{question?.problem_question_index} / 2</strong></span><div className={`timer ${timerTone}`}><small>剩余时间</small>{remaining}</div></div>
    </header>
    <div className="global-progress" aria-label={`测评进度 ${Math.round(progress)}%`}><i style={{ width: `${progress}%` }} /></div>
    <section className="material-panel">
      <article className="problem-panel"><div className="material-title">题目描述</div><div className="problem-statement">{question?.problem_statement}</div></article>
      <section className="code-panel"><div className="panel-title">本人提交代码 <span>{question?.language}</span></div><CodeBlock code={question?.source_code || ""} /></section>
    </section>
    <section className="answer-panel">
      <div className="question-heading"><span className="question-type">{questionTypeLabel(question?.question_type)}</span><span className="question-sequence">问题 {question?.question_index} / {question?.question_count}</span>{question?.question_text_en && <button type="button" className="text-button language-toggle" onClick={() => setShowEnglish(!showEnglish)}><Languages size={15} />{showEnglish ? "隐藏英文" : "显示英文对照"}</button>}</div>
      <h2>{readableGeneratedQuestion(question?.question_text)}</h2>
      {showEnglish && question?.question_text_en && <p className="question-en">{readableGeneratedQuestion(question.question_text_en)}</p>}
      <form className="answer-form" onSubmit={submit}>
        <label className="answer-label">你的回答<textarea value={answer} onChange={event => setAnswer(event.target.value)} maxLength={5000} placeholder="请结合左侧题目和代码，简洁回答当前问题。" required /></label>
        {message && <div className="error" role="alert">{message}</div>}
        <div className="form-footer"><span>{answer.length} / 5000</span><button disabled={busy || !answer.trim()}>{busy && <LoaderCircle className="spin" size={17} />}{busy ? "正在提交" : isLastQuestion ? "提交全部答案" : "提交并进入下一题"}</button></div>
      </form>
    </section>
  </main>;
}
