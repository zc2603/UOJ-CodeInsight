import { FormEvent, useEffect, useState } from "react";
import {
  ArrowLeft, BarChart3, BookOpenCheck, Check, CircleAlert, ClipboardList, Copy,
  Download, FileText, LoaderCircle, LogOut, MoreHorizontal, Plus, RefreshCw,
  RotateCcw, Search, ShieldCheck, Users, Trash2,
} from "lucide-react";
import { api } from "../api";
import { CodeBlock } from "../components/CodeBlock";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { readableGeneratedQuestion } from "../generatedText";
import type { AttemptDetail, AttemptQuestionDetail, QuizSummary, ResultRow, PreparedDetail } from "../types";

type View = "list" | "create" | "results" | "attempt" | "preparation";

interface ContestPreview {
  contest_name: string;
  submission_cutoff: string;
  cutoff_reached: boolean;
  problems: { problem_id: number; title: string }[];
  numeric_student_accounts: number;
  students_with_eligible_problem: number;
  selected_submission_snapshots: number;
  parser_errors: { student_number: string; problem_id: number; error: string }[];
}

interface ConfirmRequest {
  title: string;
  description: string;
  confirmLabel: string;
  danger?: boolean;
  action: () => Promise<void>;
}

function statusLabel(status: string | null) {
  const labels: Record<string, string> = {
    PUBLISHED: "开放中", ACTIVE: "学生作答中", DRAFT: "未开放", CLOSED: "已关闭",
    READY: "未开始", NO_ELIGIBLE_SUBMISSION: "无有效提交",
    PREPARING: "生成问题中", IN_PROGRESS: "作答中", GRADING: "评分中",
    REVIEW_REQUIRED: "待教师复核", FINISHED: "已完成", EXPIRED: "已超时", RESET: "已重置", GRADING_ERROR: "评分异常",
  };
  const normalized = status?.toUpperCase() || "";
  return normalized ? labels[normalized] || status! : "未开始";
}

function statusClass(status: string | null) {
  return (status || "ready").toLowerCase().replaceAll("_", "-");
}

function formatDate(value: string) {
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
  }).format(new Date(value));
}

function resultBucket(result: ResultRow) {
  if (result.review_required) return "attention";
  const status = (result.attempt_status || result.participant_status || "").toUpperCase();
  if (status === "FINISHED") return "finished";
  if (["PREPARING", "IN_PROGRESS", "GRADING"].includes(status)) return "active";
  if (["GRADING_ERROR", "EXPIRED"].includes(status)) return "attention";
  return "pending";
}

function groupAttemptQuestions(questions: AttemptQuestionDetail[]) {
  const groups = new Map<number, {
    problem: AttemptQuestionDetail["problem"];
    sourceCode: string;
    language: string;
    questions: AttemptQuestionDetail[];
  }>();
  for (const question of questions) {
    const current = groups.get(question.problem.id);
    if (current) current.questions.push(question);
    else groups.set(question.problem.id, {
      problem: question.problem,
      sourceCode: question.source_code,
      language: question.language,
      questions: [question],
    });
  }
  return [...groups.values()];
}

export function AdminApp() {
  const [loggedIn, setLoggedIn] = useState(false);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [message, setMessage] = useState("");
  const [toast, setToast] = useState("");
  const [busy, setBusy] = useState(false);
  const [view, setView] = useState<View>("list");
  const [quizzes, setQuizzes] = useState<QuizSummary[]>([]);
  const [results, setResults] = useState<ResultRow[]>([]);
  const [selected, setSelected] = useState<QuizSummary | null>(null);
  const [preparedDetail, setPreparedDetail] = useState<PreparedDetail | null>(null);
  const [attempt, setAttempt] = useState<AttemptDetail | null>(null);
  const [overrideScore, setOverrideScore] = useState(0);
  const [overrideReason, setOverrideReason] = useState("");
  const [contestId, setContestId] = useState("");
  const [preview, setPreview] = useState<ContestPreview | null>(null);
  const [createdCode, setCreatedCode] = useState("");
  const [confirm, setConfirm] = useState<ConfirmRequest | null>(null);
  const [resultQuery, setResultQuery] = useState("");
  const [resultFilter, setResultFilter] = useState<"all" | "finished" | "active" | "attention" | "pending">("all");

  const attemptGroups = attempt ? groupAttemptQuestions(attempt.questions) : [];
  const [studentCount, setStudentCount] = useState<number | null>(null);
  const totalFinished = quizzes.reduce((sum, quiz) => sum + quiz.finished_count, 0);
  const filteredResults = results.filter(result => {
    const matchesQuery = result.student_number.includes(resultQuery.trim());
    const matchesStatus = resultFilter === "all" || resultBucket(result) === resultFilter;
    return matchesQuery && matchesStatus;
  });

  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(""), 3200);
    return () => window.clearTimeout(timer);
  }, [toast]);

  async function copyText(value: string, successMessage: string) {
    try {
      if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(value);
      else {
        const textarea = document.createElement("textarea");
        textarea.value = value;
        textarea.style.position = "fixed";
        textarea.style.opacity = "0";
        document.body.appendChild(textarea);
        textarea.focus();
        textarea.select();
        if (!document.execCommand("copy")) throw new Error("copy failed");
        textarea.remove();
      }
      setToast(successMessage);
    } catch {
      setMessage(`复制失败，请手动复制：${value}`);
    }
  }

  function studentLink(quizId: string) {
    return `${window.location.origin}/q/${quizId}`;
  }

  async function loadQuizzes() {
    try {
      const [data, overview] = await Promise.all([api<QuizSummary[]>("/api/admin/quizzes"), api<{ student_count: number }>("/api/admin/overview")]);
      setQuizzes(data);
      setStudentCount(overview.student_count);
      setLoggedIn(true);
    } catch {
      setLoggedIn(false);
    }
  }

  async function refreshQuizzes() {
    setBusy(true);
    setMessage("");
    try {
      const [data, overview] = await Promise.all([api<QuizSummary[]>("/api/admin/quizzes"), api<{ student_count: number }>("/api/admin/overview")]);
      setQuizzes(data);
      setStudentCount(overview.student_count);
      setToast("测评列表已更新");
    } catch (error) {
      setMessage((error as Error).message);
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => { void loadQuizzes(); }, []);

  useEffect(() => {
    if (view !== "results" || !selected) return;
    const timer = window.setInterval(() => {
      void api<ResultRow[]>(`/api/admin/quizzes/${selected.id}/results`).then(setResults).catch(() => undefined);
    }, 10000);
    return () => window.clearInterval(timer);
  }, [view, selected]);

  useEffect(() => {
    if (!loggedIn || view !== "list") return;
    let cancelled = false;
    let timer: number;
    async function poll() {
      try {
        const data = await api<QuizSummary[]>("/api/admin/quizzes");
        if (!cancelled) setQuizzes(data);
      } catch {
        if (!cancelled) setMessage("进度更新失败，请刷新重试。");
      }
      if (!cancelled) timer = window.setTimeout(poll, 5000);
    }
    timer = window.setTimeout(poll, 5000);
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [loggedIn, view]);

  function preparationAction(quiz: QuizSummary, retry = false) {
    setConfirm({
      title: retry ? "重试未完成的出题？" : "现在开放测评？",
      description: retry ? "仅重试失败的题目，已经完成的问题会保留。重试会产生模型调用费用。" : "开放后，学生有 30 分钟进入测评。每位学生从开始作答时独立计时。",
      confirmLabel: retry ? "重试出题" : "开放测评",
      action: async () => {
        setBusy(true);
        setMessage("");
        try {
          await api(`/api/admin/quizzes/${quiz.id}/${retry ? "retry-preparation" : "open"}`, { method: "POST" });
          await loadQuizzes();
          setToast(retry ? "已安排重试" : "测评已开放，可通知学生进入");
        } catch (error) { setMessage((error as Error).message); }
        finally { setBusy(false); }
      },
    });
  }

  async function login(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setMessage("");
    try {
      await api("/api/admin/login", { method: "POST", body: JSON.stringify({ username, password }) });
      setPassword("");
      await loadQuizzes();
    } catch (error) {
      setMessage((error as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function doPreview(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setMessage("");
    setCreatedCode("");
    try {
      setPreview(await api<ContestPreview>("/api/admin/quizzes/preview-contest", {
        method: "POST", body: JSON.stringify({ contest_id: Number(contestId) }),
      }));
    } catch (error) {
      setMessage((error as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function performCreateQuiz(allowBeforeCutoff: boolean) {
    setBusy(true);
    setMessage("");
    try {
      const data = await api<{ id: string; quiz_code: string }>("/api/admin/quizzes", {
        method: "POST",
        body: JSON.stringify({
          contest_id: Number(contestId), show_score_after_finish: false, allow_before_cutoff: allowBeforeCutoff,
        }),
      });
      setCreatedCode(data.quiz_code);
      setSelected({ id: data.id, name: preview?.contest_name || `Contest ${contestId}` } as QuizSummary);
      await loadQuizzes();
      setView("list");
      setToast("测评已创建，正在准备问题");
    } catch (error) {
      setMessage((error as Error).message);
    } finally {
      setBusy(false);
    }
  }

  function createQuiz() {
    const creatingBeforeCutoff = preview !== null && !preview.cutoff_reached;
    if (!creatingBeforeCutoff) return void performCreateQuiz(false);
    setConfirm({
      title: "提前创建这场测评？",
      description: "Contest 尚未结束。本次测评将采用学生当前的提交；如需包含后续提交，请在比赛结束后创建。",
      confirmLabel: "确认提前创建",
      action: () => performCreateQuiz(true),
    });
  }

  async function performRegenerateCode(quiz: QuizSummary) {
    setBusy(true);
    setMessage("");
    try {
      const data = await api<{ quiz_code: string }>(`/api/admin/quizzes/${quiz.id}/regenerate-code`, { method: "POST" });
      setCreatedCode(data.quiz_code);
      setSelected(quiz);
      setToast("备用测评码已重新生成");
    } catch (error) {
      setMessage((error as Error).message);
    } finally {
      setBusy(false);
    }
  }

  function deleteQuiz(quiz: QuizSummary) {
    setConfirm({
      title: `删除“${quiz.name}”？`,
      description: "将删除本场测评的题目、学生作答和成绩，并取消未完成的出题任务。删除后无法撤销。",
      confirmLabel: "删除此测评", danger: true,
      action: async () => {
        setBusy(true);
        setMessage("");
        try {
          await api(`/api/admin/quizzes/${quiz.id}`, { method: "DELETE" });
          setQuizzes(current => current.filter(item => item.id !== quiz.id));
          if (selected?.id === quiz.id) { setSelected(null); setCreatedCode(""); setResults([]); setAttempt(null); }
          await loadQuizzes();
          setToast("测评已删除");
        } catch (error) { setMessage((error as Error).message); }
        finally { setBusy(false); }
      },
    });
  }

  function regenerateCode(quiz: QuizSummary) {
    setConfirm({
      title: "重新生成备用测评码？",
      description: `“${quiz.name}”当前的备用测评码将立即失效，已经取得旧码的用户将无法继续使用。`,
      confirmLabel: "重新生成",
      action: () => performRegenerateCode(quiz),
    });
  }

  async function showResults(quiz: QuizSummary) {
    setBusy(true);
    setMessage("");
    setSelected(quiz);
    try {
      setResults(await api(`/api/admin/quizzes/${quiz.id}/results`));
      setView("results");
    } catch (error) {
      setMessage((error as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function loadPrepared(studentNumber: string) {
    if (!selected) return;
    setBusy(true);
    setMessage("");
    try {
      const detail = await api<PreparedDetail>(`/api/admin/quizzes/${selected.id}/students/${encodeURIComponent(studentNumber)}/prepared-questions`);
      setPreparedDetail(detail);
      setView("preparation");
    } catch (error) { setMessage((error as Error).message); }
    finally { setBusy(false); }
  }

  async function loadAttempt(attemptId: string) {
    setBusy(true);
    setMessage("");
    try {
      const detail = await api<AttemptDetail>(`/api/admin/attempts/${attemptId}`);
      setAttempt(detail);
      setOverrideScore((detail.review_required ? detail.auto_score : detail.manual_override_score ?? detail.auto_score) ?? 0);
      setOverrideReason(detail.manual_override_reason ?? "");
      setView("attempt");
    } catch (error) {
      setMessage((error as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function saveOverride(event: FormEvent) {
    event.preventDefault();
    if (!attempt) return;
    setBusy(true);
    setMessage("");
    try {
      await api(`/api/admin/attempts/${attempt.id}/score`, {
        method: "PATCH", body: JSON.stringify({ score: overrideScore, reason: overrideReason }),
      });
      await loadAttempt(attempt.id);
      setToast("人工覆盖分数已保存");
    } catch (error) {
      setMessage((error as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function performRegrade() {
    if (!attempt) return;
    setBusy(true);
    setMessage("");
    try {
      await api(`/api/admin/attempts/${attempt.id}/regrade`, { method: "POST" });
      await loadAttempt(attempt.id);
      setToast("重新评分已完成");
    } catch (error) {
      setMessage((error as Error).message);
    } finally {
      setBusy(false);
    }
  }

  function regrade() {
    if (!attempt) return;
    setConfirm({
      title: "重新进行自动评分？",
      description: "将根据已提交的答案重新评分，并更新自动成绩。此操作会产生模型调用费用。",
      confirmLabel: "重新评分",
      action: performRegrade,
    });
  }

  async function performResetAttempt() {
    if (!attempt) return;
    setBusy(true);
    setMessage("");
    try {
      await api(`/api/admin/attempts/${attempt.id}/reset`, { method: "POST" });
      setToast("重置成功，请在问题准备完成后重新作答");
      if (selected) await showResults(selected);
    } catch (error) {
      setMessage((error as Error).message);
    } finally {
      setBusy(false);
    }
  }

  function resetAttempt() {
    if (!attempt) return;
    setConfirm({
      title: "允许该学生重新作答？",
      description: "旧答案和评分记录会保留，并重新准备该学生的问题。重新出题会产生模型调用费用，请为准备和重新进入预留时间。",
      confirmLabel: "确认重置", danger: true, action: performResetAttempt,
    });
  }

  async function runConfirmedAction() {
    if (!confirm) return;
    const action = confirm.action;
    await action();
    setConfirm(null);
  }

  async function logout() {
    setBusy(true);
    try {
      await api("/api/admin/logout", { method: "POST" });
    } finally {
      setLoggedIn(false);
      setUsername("");
      setPassword("");
      setBusy(false);
    }
  }

  if (!loggedIn) return (
    <main className="center-page auth-page">
      <div className="auth-brand">
        <span className="brand-mark"><BookOpenCheck size={24} /></span>
        <span><strong>UOJ 课程辅助系统</strong><small>代码理解测评平台</small></span>
      </div>
      <form className="card login-card" onSubmit={login}>
        <div className="role-label"><ShieldCheck size={16} /> 教师管理端</div>
        <h1>欢迎回来</h1>
        <p className="muted">登录后可创建测评、查看进度并复核评分。</p>
        <label>用户名<input autoComplete="username" value={username} onChange={event => setUsername(event.target.value)} placeholder="请输入教师用户名" required /></label>
        <label>密码<input type="password" autoComplete="current-password" value={password} onChange={event => setPassword(event.target.value)} placeholder="请输入密码" required /></label>
        {message && <div className="error" role="alert">{message}</div>}
        <button disabled={busy}>{busy && <LoaderCircle className="spin" size={17} />}{busy ? "正在登录" : "登录管理端"}</button>
      </form>
      <div className="auth-footer">课程内网服务 · 请使用教师账号访问</div>
    </main>
  );

  return (
    <div className="admin-shell">
      <aside>
        <div className="admin-brand">
          <span className="brand-mark compact"><BookOpenCheck size={20} /></span>
          <span><strong>代码理解测评</strong><small>UOJ 课程辅助系统</small></span>
        </div>
        <nav aria-label="教师端主导航">
          <button className={view !== "create" ? "active" : ""} onClick={() => setView("list")}><ClipboardList size={18} />测评管理</button>
          <button className={view === "create" ? "active" : ""} onClick={() => setView("create")}><Plus size={18} />创建测评</button>
        </nav>
        <div className="admin-aside-footer"><div><BookOpenCheck size={15} /><span><strong>课程测评</strong><small>教师工作台</small></span></div><button type="button" onClick={() => void logout()} title="退出登录"><LogOut size={16} />退出</button></div>
      </aside>

      <main className="admin-main">
        {message && <div className="page-error error" role="alert"><CircleAlert size={17} />{message}</div>}

        {view === "list" && <>
          <header>
            <div><div className="eyebrow">QUIZ MANAGEMENT</div><h1>测评管理</h1><p>查看开放状态、学生参与进度与评分结果。</p></div>
            <div className="header-actions"><button className="secondary" onClick={() => void refreshQuizzes()} disabled={busy}><RefreshCw className={busy ? "spin" : ""} size={16} />刷新数据</button><button onClick={() => setView("create")}><Plus size={16} />创建测评</button></div>
          </header>

          {createdCode && selected && <div className="code-reveal floating-code">
            <div className="reveal-symbol"><Check size={18} /></div>
            <div><span>“{selected.name}”的备用测评码</span><strong>{createdCode}</strong><small>学生无法使用 UOJ 密码登录时，可提供此备用码。</small></div>
            <div className="inline-actions"><button className="secondary" onClick={() => void copyText(createdCode, "备用测评码已复制")}><Copy size={16} />复制备用码</button><button onClick={() => void copyText(studentLink(selected.id), "学生链接已复制")}><Copy size={16} />复制学生链接</button></div>
          </div>}

          <section className="summary-strip" aria-label="测评概览">
            <div><span>测评总数</span><strong>{quizzes.length}</strong></div>
            <div><span>当前开放</span><strong>{quizzes.filter(quiz => quiz.status.toUpperCase() === "PUBLISHED").length}</strong></div>
            <div><span>学生总数</span><strong>{studentCount ?? "—"}</strong></div>
            <div><span>已完成作答</span><strong>{totalFinished}</strong></div>
          </section>

          {quizzes.filter(quiz => quiz.preparation && (quiz.status.toUpperCase() === "DRAFT" || !quiz.preparation.ready)).map(quiz => {
            const p = quiz.preparation!;
            return <section className="card preparation-card" key={quiz.id} aria-label={`${quiz.name}出题进度`}>
              <div className="preparation-heading"><div><span className="eyebrow">课前准备</span><h2>{quiz.name}</h2></div><span className={`status-pill status-${p.ready ? "finished" : p.failed ? "grading-error" : "preparing"}`}>{p.ready ? "已就绪" : p.total === 0 ? "无有效提交" : p.failed ? "部分出题失败" : "正在出题"}</span></div>
              <div className="preparation-counts"><div><strong>{p.completed}<small> / {p.total}</small></strong><span>已完成出题的有效题目</span></div><div><strong>{p.students_ready}<small> / {p.students_total}</small></strong><span>问题全部就绪的学生</span></div></div>
              <progress max={p.total || 1} value={p.completed} aria-label="已完成出题的有效题目" />
              <div className="preparation-footer"><span>{p.ready ? `已准备 ${p.completed * 2} 个问题，可以开放测评。` : p.total === 0 ? "本场没有可用于出题的提交，请核对 Contest 后重新创建。" : `出题中 ${p.running} · 等待 ${p.queued} · 失败 ${p.failed}（每道有效题目生成 2 问）`}</span><div className="inline-actions">{p.failed > 0 && <button className="secondary" disabled={busy} onClick={() => preparationAction(quiz, true)}>重试失败题目</button>}{quiz.status.toUpperCase() === "DRAFT" && <button disabled={busy || !p.ready} onClick={() => preparationAction(quiz)}>开放测评</button>}</div></div>
            </section>;
          })}

          <section className="card table-card">
            {quizzes.length ? <table className="quiz-table">
              <thead><tr><th>测评</th><th>Contest</th><th>最后进入时间</th><th>完成进度</th><th>平均分</th><th>操作</th></tr></thead>
              <tbody>{quizzes.map(quiz => {
                const progress = quiz.participant_count ? Math.min(100, quiz.finished_count / quiz.participant_count * 100) : 0;
                return <tr key={quiz.id}>
                  <td><div className="quiz-name-cell"><span className={`status-dot dot-${statusClass(quiz.status)}`} /><div><strong>{quiz.name}</strong><small>{statusLabel(quiz.status)}</small></div></div></td>
                  <td className="mono">#{quiz.uoj_contest_id}</td>
                  <td>{quiz.status.toUpperCase() === "DRAFT" ? "开放后 30 分钟" : formatDate(quiz.end_time)}</td>
                  <td><div className="table-progress"><span><strong>{quiz.finished_count}</strong> / {quiz.participant_count}</span><i><b style={{ width: `${progress}%` }} /></i></div></td>
                  <td><strong>{quiz.average_score !== null ? `${quiz.average_score.toFixed(1)}%` : "—"}</strong></td>
                  <td className="actions-cell"><div className="actions"><button className="link primary-link" onClick={() => void showResults(quiz)}>查看结果</button><details className="action-menu" name="quiz-actions"><summary aria-label="更多操作"><MoreHorizontal size={18} /></summary><div onClick={event => { const menu = event.currentTarget.closest("details"); if (menu) menu.open = false; }}><button onClick={() => void copyText(studentLink(quiz.id), "学生链接已复制")}><Copy size={15} />复制学生链接</button><button disabled={busy} onClick={() => regenerateCode(quiz)}><RefreshCw size={15} />生成备用码</button><button className="delete-quiz" disabled={busy} onClick={() => deleteQuiz(quiz)}><Trash2 size={15} />删除此测评</button></div></details></div></td>
                </tr>;
              })}</tbody>
            </table> : <div className="empty-state"><div className="empty-symbol"><ClipboardList size={27} /></div><h2>还没有测评</h2><p>创建第一场测评后，这里会显示学生进入、作答和评分进度。</p><button onClick={() => setView("create")}><Plus size={16} />创建第一场测评</button></div>}
          </section>
        </>}

        {view === "create" && <>
          <header><div><div className="eyebrow">NEW QUIZ</div><h1>创建测评</h1><p>从 UOJ 比赛创建一场代码理解测评。</p></div></header>
          <div className="create-flow">
            <form className={`card flow-card ${preview ? "complete" : "active"}`} onSubmit={doPreview}>
              <div className="section-rail"><div className="section-number">{preview ? <Check size={17} /> : "1"}</div><i /></div>
              <div className="section-body"><div className="step-label">第一步</div><h2>导入比赛</h2><p className="muted">输入 Contest ID，导入本次测评的比赛信息。</p><label>Contest ID<input type="number" min="1" value={contestId} onChange={event => { setContestId(event.target.value); setPreview(null); setCreatedCode(""); }} placeholder="例如：1" required /><small>即 UOJ 比赛页面网址末尾的数字</small></label><button disabled={busy}>{busy && <LoaderCircle className="spin" size={16} />}{busy ? "正在导入" : preview ? "重新导入" : "导入并预览"}</button></div>
            </form>

            <section className={`card flow-card ${preview ? "active" : "pending"}`}>
              <div className="section-rail"><div className="section-number">2</div></div>
              <div className="section-body">
                <div className="step-label">第二步</div><h2>确认并准备</h2>
                {!preview ? <div className="pending-copy"><FileText size={22} /><p>导入后，请在这里确认题目和学生人数。</p></div> : <>
                  <div className="preview-heading"><div><h3>{preview.contest_name}</h3><p>提交截止：{new Date(preview.submission_cutoff).toLocaleString()}</p></div><span className={`status-pill ${preview.cutoff_reached ? "status-finished" : "status-preparing"}`}>{preview.cutoff_reached ? "已结束" : "进行中"}</span></div>
                  {!preview.cutoff_reached && <div className="warning"><CircleAlert size={17} /><div><strong>Contest 尚未结束</strong><span>提前创建将采用学生当前的提交。如需包含后续提交，请在比赛结束后创建。</span></div></div>}
                  <div className="stats"><div><strong>{preview.problems.length}</strong><span>比赛题目</span></div><div><strong>{preview.numeric_student_accounts}</strong><span>学生人数</span></div><div><strong>{preview.students_with_eligible_problem}</strong><span>可参与人数</span></div><div><strong>{preview.parser_errors.length}</strong><span>导入异常</span></div></div>
                  <div className="problem-chip-list">{preview.problems.map(problem => <span key={problem.problem_id}><b>#{problem.problem_id}</b>{problem.title}</span>)}</div>
                  {preview.parser_errors.length > 0 && <details className="parser-errors"><summary>查看 {preview.parser_errors.length} 条导入异常</summary><ul>{preview.parser_errors.map((error, index) => <li key={`${error.student_number}-${error.problem_id}-${index}`}>{error.student_number} · 题目 {error.problem_id}：{error.error}</li>)}</ul></details>}
                  <div className="policy-box"><div><Check size={16} /><span>创建后开始出题</span></div><div><Check size={16} /><span>教师开放后 30 分钟内进入</span></div><div><Check size={16} /><span>每道有效题目固定 2 问</span></div><div><Check size={16} /><span>每个问题 3 分钟</span></div></div>
                  <p className="muted">将为 {preview.selected_submission_snapshots} 道有效题目准备问题，产生相应的模型调用费用。出题完成后，由你决定何时开放。</p>
                  <button className="create-submit" onClick={createQuiz} disabled={busy}>{busy && <LoaderCircle className="spin" size={16} />}{busy ? "正在创建" : preview.cutoff_reached ? "创建并准备问题" : "提前创建并准备问题"}</button>
                  {createdCode && selected && <div className="code-reveal"><div className="reveal-symbol"><Check size={18} /></div><div><span>测评已创建</span><small>复制学生链接即可发布；备用码只需在登录异常时提供。</small></div><div className="inline-actions"><button onClick={() => void copyText(studentLink(selected.id), "学生链接已复制")}><Copy size={16} />复制学生链接</button><details><summary>备用码</summary><strong>{createdCode}</strong><button className="secondary" onClick={() => void copyText(createdCode, "备用测评码已复制")}>复制</button></details></div></div>}
                </>}
              </div>
            </section>
          </div>
        </>}

        {view === "results" && selected && <>
          <header>
            <div><button className="back" onClick={() => setView("list")}><ArrowLeft size={15} />返回测评管理</button><h1>{selected.name}</h1><p><span className="live-dot" />结果每 10 秒自动刷新</p></div>
            <a className="button secondary" href={`/api/admin/quizzes/${selected.id}/export`}><Download size={16} />导出 CSV</a>
          </header>
          <section className="result-overview">
            <div><Users size={18} /><span>学生总数<strong>{results.length}</strong></span></div>
            <div><Check size={18} /><span>已完成<strong>{results.filter(result => !result.review_required && result.attempt_status?.toUpperCase() === "FINISHED").length}</strong></span></div>
            <div><BarChart3 size={18} /><span>已评分平均分<strong>{(() => { const values = results.flatMap(result => result.final_percent === null ? [] : [result.final_percent]); return values.length ? `${(values.reduce((sum, value) => sum + value, 0) / values.length).toFixed(1)}%` : "—"; })()}</strong></span></div>
          </section>
          <section className="card table-card result-card">
            {results.length ? <><div className="result-toolbar"><label><Search size={15} /><input aria-label="搜索学号" value={resultQuery} onChange={event => setResultQuery(event.target.value)} placeholder="搜索学号" /></label><div className="filter-tabs" role="group" aria-label="筛选作答状态"><button className={resultFilter === "all" ? "active" : ""} onClick={() => setResultFilter("all")}>全部</button><button className={resultFilter === "finished" ? "active" : ""} onClick={() => setResultFilter("finished")}>已完成</button><button className={resultFilter === "active" ? "active" : ""} onClick={() => setResultFilter("active")}>进行中</button><button className={resultFilter === "pending" ? "active" : ""} onClick={() => setResultFilter("pending")}>未作答</button><button className={resultFilter === "attention" ? "active" : ""} onClick={() => setResultFilter("attention")}>需关注</button></div><span>{filteredResults.length} / {results.length} 人</span></div>{filteredResults.length ? <table className="result-table"><thead><tr><th>学号</th><th>覆盖题目</th><th>问题数</th><th>原始分</th><th>百分制</th><th>最低置信度</th><th>状态</th><th></th></tr></thead><tbody>{filteredResults.map(result => <tr key={result.student_number}><td className="mono student-number">{result.student_number}</td><td>{result.problem_count}</td><td>{result.question_count}</td><td><strong>{result.final_score ?? "—"}{result.max_score ? ` / ${result.max_score}` : ""}</strong>{result.review_required && <small>建议分 {result.auto_score ?? "—"}</small>}{result.manual_score !== null && <small>人工覆盖</small>}</td><td><strong className="percent-score">{result.final_percent !== null ? `${result.final_percent.toFixed(1)}%` : "—"}</strong></td><td>{result.confidence?.toFixed(2) ?? "—"}</td><td><span className={`status-pill status-${statusClass(result.review_required ? "GRADING_ERROR" : result.attempt_status || result.participant_status)}`}>{statusLabel(result.review_required ? "REVIEW_REQUIRED" : result.attempt_status || result.participant_status)}</span></td><td>{result.attempt_id && <button className="link" disabled={busy} onClick={() => void loadAttempt(result.attempt_id!)}>查看详情</button>}{(!result.attempt_id || result.attempt_status === "RESET") && result.prepared_problem_count > 0 && <button className="link" disabled={busy} onClick={() => void loadPrepared(result.student_number)}>{result.attempt_id ? "查看新题" : "查看详情"}</button>}</td></tr>)}</tbody></table> : <div className="filtered-empty">没有符合当前条件的学生</div>}</> : <div className="empty-state compact-empty"><div className="empty-symbol"><Users size={25} /></div><h2>暂时没有作答记录</h2><p>学生进入并开始测评后，进度会自动显示在这里。</p></div>}
          </section>
        </>}

        {view === "preparation" && preparedDetail && <>
          <header><div><button className="back" onClick={() => setView("results")}><ArrowLeft size={15} />返回测评结果</button><div className="eyebrow">QUESTION PREVIEW</div><h1 className="mono">{preparedDetail.student_number}</h1><p>已准备 {preparedDetail.prepared_problem_count} / {preparedDetail.preparation_total} 道题目，共 {preparedDetail.questions.length} 个问题</p></div><span className="status-pill status-ready">预生成题目</span></header>
          <div className="problem-review-list">{groupAttemptQuestions(preparedDetail.questions).map(group => <section className="card problem-review" key={group.problem.id}>
            <header><div><span className="problem-number">题目 {group.problem.id}</span><h2>{group.problem.title}</h2></div><span className="question-count">{group.questions.length} 个问题</span></header>
            <details className="material-details"><summary><FileText size={15} />查看题面与提交代码</summary><div className="review-material"><div className="statement">{group.problem.statement}</div><div className="source-card"><CodeBlock code={group.sourceCode} language={group.language} /></div></div></details>
            <div className="question-list">{group.questions.map((question, index) => <article className="question-detail" key={question.index}>
              <div className="question-title-row"><div className="question-index">问题 {index + 1}</div></div><h3>{readableGeneratedQuestion(question.question)}</h3>
              {question.question_en && <p className="question-en">{readableGeneratedQuestion(question.question_en)}</p>}
              <dl><dt>参考答案</dt><dd>{question.reference_answer}</dd><dt>出题评分点</dt><dd>{question.grading_points.join("；")}</dd></dl>
            </article>)}</div>
          </section>)}</div>
        </>}

        {view === "attempt" && attempt && <>
          <header>
            <div><button className="back" onClick={() => setView("results")}><ArrowLeft size={15} />返回测评结果</button><div className="eyebrow">STUDENT REVIEW</div><h1 className="mono">{attempt.student_number}</h1><p>共 {attemptGroups.length} 道题、{attempt.questions.length} 个问题 · <span className={`status-pill status-${statusClass(attempt.review_required ? "GRADING_ERROR" : attempt.status)}`}>{statusLabel(attempt.review_required ? "REVIEW_REQUIRED" : attempt.status)}</span></p></div>
            <div className="header-actions"><button className="secondary" disabled={busy} onClick={regrade}><RefreshCw size={16} />重新评分</button><button className="danger" disabled={busy} onClick={resetAttempt}><RotateCcw size={16} />允许重新作答</button></div>
          </header>
          <section className="detail-grid">
            <div className="problem-review-list">{attemptGroups.map(group => <section className="card problem-review" key={group.problem.id}>
              <header><div><span className="problem-number">题目 {group.problem.id}</span><h2>{group.problem.title}</h2></div><span className="question-count">{group.questions.length} 个问题</span></header>
              <details className="material-details"><summary><FileText size={15} />查看题面与提交代码</summary><div className="review-material"><div className="statement">{group.problem.statement}</div><div className="source-card"><CodeBlock code={group.sourceCode} language={group.language} /></div></div></details>
              <div className="question-list">{group.questions.map((question, index) => <article className="question-detail" key={question.index}>
                <div className="question-title-row"><div className="question-index">问题 {index + 1}</div><span className="question-score">{attempt.review_required ? "建议 " : ""}{question.score ?? "—"} / 2</span></div>
                <h3>{readableGeneratedQuestion(question.question)}</h3>
                {question.review_required && <p className="review-notice">{attempt.review_required ? "待教师复核" : "曾触发复核"}：{question.review_reason}</p>}
                {question.question_en && <p className="question-en">{readableGeneratedQuestion(question.question_en)}</p>}
                <dl><dt>学生回答</dt><dd className="student-answer">{question.student_answer ?? "尚未回答"}</dd><dt>评分原因</dt><dd>{question.reason ?? "—"}</dd><dt>置信度</dt><dd>{question.confidence?.toFixed(2) ?? "—"}</dd><dt>参考答案</dt><dd>{question.reference_answer}</dd></dl>
              </article>)}</div>
            </section>)}</div>
            <div className="card override-card"><div className="override-heading"><h2>成绩复核</h2><ShieldCheck size={19} /></div>{attempt.review_required && <p className="review-notice">本次评分待复核，建议分尚未计入最终成绩。请检查题目和评分依据，填写总分与原因后保存；重新评分不会自动解除待复核状态。</p>}<div className="score compact">{(attempt.review_required ? attempt.auto_score : attempt.manual_override_score ?? attempt.auto_score) ?? "—"}<small> / {attempt.max_score}</small></div><div className="percent-large">{attempt.final_percent !== null ? `${attempt.final_percent.toFixed(1)}%` : "—"}</div><div className="score-meta"><span>{attempt.review_required ? "待复核建议分" : "当前百分制"}</span><span>自动总分 {attempt.auto_score ?? "—"}</span></div><form onSubmit={saveOverride}><label>人工覆盖分数<input type="number" min="0" max={attempt.max_score} value={overrideScore} onChange={event => setOverrideScore(Number(event.target.value))} /></label><label>覆盖原因<textarea required maxLength={5000} value={overrideReason} onChange={event => setOverrideReason(event.target.value)} placeholder="请填写复核依据" /></label><button disabled={busy}>{busy && <LoaderCircle className="spin" size={16} />}保存复核结果</button></form></div>
          </section>
        </>}
      </main>

      {toast && <div className="toast" role="status"><Check size={17} />{toast}</div>}
      {confirm && <ConfirmDialog title={confirm.title} description={confirm.description} confirmLabel={confirm.confirmLabel} danger={confirm.danger} busy={busy} onCancel={() => setConfirm(null)} onConfirm={() => void runConfirmedAction()} />}
    </div>
  );
}
