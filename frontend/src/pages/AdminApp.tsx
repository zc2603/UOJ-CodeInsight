import { FormEvent, useEffect, useState } from "react";
import {
  ArrowLeft, BarChart3, BookOpenCheck, Check, CircleAlert, ClipboardList, Copy,
  Download, FileText, LoaderCircle, LogOut, MoreHorizontal, Plus, RefreshCw,
  RotateCcw, Search, ShieldCheck, Users, Trash2,
} from "lucide-react";
import { api } from "../api";
import { scoreGrade } from "../scoreDisplay";
import { ProblemStatement } from "../components/ProblemStatement";
import { QualityAudit } from "../components/QualityAudit";
import { AssessmentSetup } from "../components/AssessmentSetup";
import { CodeBlock } from "../components/CodeBlock";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { AppealPanel, AppealReviewDialog, type AppealItem } from "../components/AppealPanel";
import type { AttemptDetail, AttemptQuestionDetail, QuizSummary, ResultRow, PreparedDetail } from "../types";

function readableReviewReason(reason: string | null | undefined) {
  return reason?.replaceAll("评分理由涉及空白差异，需教师核对建议分", "系统提醒：评分理由提到了空格或换行，请确认这些格式要求是否影响本题得分");
}

type View = "list" | "create" | "results" | "attempt" | "preparation";

interface ContestPreview {
  roster: null | { requested_count: number; duplicate_count: number; matched_students: string[];
    unknown_students: string[]; unavailable_students: string[]; selected_submission_snapshots: number };
  contest_name: string;
  submission_cutoff: string;
  cutoff_reached: boolean;
  problems: { problem_id: number; title: string; display_order?: number; include_choice?: boolean }[];
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
  if (result.review_required || result.quality_attention) return "attention";
  const status = (result.attempt_status || result.participant_status || "").toUpperCase();
  if (status === "FINISHED") return "finished";
  if (["PREPARING", "IN_PROGRESS", "GRADING"].includes(status)) return "active";
  if (["GRADING_ERROR", "EXPIRED"].includes(status)) return "attention";
  return "pending";
}

function confirmedResultScore(result: ResultRow) {
  return !result.review_required && result.attempt_status?.toUpperCase() === "FINISHED"
    && result.question_count > 0 ? result.final_score : null;
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
  const [editingQuestion, setEditingQuestion] = useState<AttemptQuestionDetail | null>(null);
  const [editError, setEditError] = useState("");
  const [preparedDetail, setPreparedDetail] = useState<PreparedDetail | null>(null);
  const [attempt, setAttempt] = useState<AttemptDetail | null>(null);
  const [overrideScore, setOverrideScore] = useState(0);
  const [overrideReason, setOverrideReason] = useState("");
  const [contestId, setContestId] = useState("");
  const [rosterText, setRosterText] = useState<string | null>(null);
  const [rosterName, setRosterName] = useState("");
  const [rosterError, setRosterError] = useState("");
  const [preview, setPreview] = useState<ContestPreview | null>(null);
  const [lightweightEnabled, setLightweightEnabled] = useState(false);
  const [qualityAuditEnabled, setQualityAuditEnabled] = useState(false);
  const [choiceIds, setChoiceIds] = useState<number[]>([]);
  const [timeMode, setTimeMode] = useState<"per_question" | "fixed">("per_question");
  const [perQuestionMinutes, setPerQuestionMinutes] = useState(5);
  const [fixedMinutes, setFixedMinutes] = useState(25);
  const [createdCode, setCreatedCode] = useState("");
  const [confirm, setConfirm] = useState<ConfirmRequest | null>(null);
  const [resultQuery, setResultQuery] = useState("");
  const [resultFilter, setResultFilter] = useState<"all" | "finished" | "active" | "attention" | "pending">("all");
  const [appeals, setAppeals] = useState<AppealItem[]>([]);
  const [resolvingAppeal, setResolvingAppeal] = useState<string | null>(null);
  const [appealResolution, setAppealResolution] = useState("");
  const [appealScore, setAppealScore] = useState<string>("");
  const [appealError, setAppealError] = useState("");
  const [editingScoreQuestion, setEditingScoreQuestion] = useState<string | null>(null);
  const [questionScore, setQuestionScore] = useState(0);
  const [questionScoreReason, setQuestionScoreReason] = useState("");

  const attemptGroups = attempt ? groupAttemptQuestions(attempt.questions) : [];
  const [studentCount, setStudentCount] = useState<number | null>(null);
  const totalFinished = quizzes.reduce((sum, quiz) => sum + quiz.finished_count, 0);
  const activeAppeal = appeals.find(appeal => appeal.id === resolvingAppeal);
  const studentForAttempt = (id: string) => results.find(result => result.attempt_id === id)?.student_number ?? "学生申诉";
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
      void api<{ lightweight_creation_enabled: boolean; quality_audit_enabled: boolean }>("/api/admin/features")
        .then(features => { setLightweightEnabled(features.lightweight_creation_enabled);
          setQualityAuditEnabled(features.quality_audit_enabled); }).catch(() => undefined);
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
      void api<AppealItem[]>(`/api/admin/quizzes/${selected.id}/appeals`).then(setAppeals).catch(() => undefined);
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

  function preparationAction(quiz: QuizSummary, action: "open" | "retry" | "stop" = "open") {
    const p = quiz.preparation;
    const partial = action === "open" && !p?.ready;
    setConfirm({
      title: action === "stop" ? "终止本场出题？" : action === "retry" ? "重试未完成的出题？" : partial ? "部分问题未完成，仍要发布？" : "现在开放测评？",
      description: action === "stop" ? "已完成的问题会保留，等待中和正在生成的任务将终止。已发送给模型的请求仍可能计费。" : action === "retry" ? "重新生成失败或已终止的题目，已经完成的问题会保留。重试会产生模型调用费用。" : partial ? `目前 ${p?.students_ready ?? 0} 名学生的整套问题已就绪，可参加测评；另有 ${(p?.students_total ?? 0) - (p?.students_ready ?? 0)} 名学生暂不能参加。剩余出题任务将终止。发布后，学生有 30 分钟进入测评。` : "开放后，学生有 30 分钟进入测评。每位学生从开始作答时独立计时。",
      confirmLabel: action === "stop" ? "终止出题" : action === "retry" ? "重试出题" : partial ? "确认发布" : "开放测评",
      action: async () => {
        setBusy(true);
        setMessage("");
        try {
          await api(`/api/admin/quizzes/${quiz.id}/${action === "retry" ? "retry-preparation" : action === "stop" ? "stop-preparation" : "open"}`, {
            method: "POST", ...(action === "open" ? { body: JSON.stringify({ confirm_partial: partial }) } : {}),
          });
          await loadQuizzes();
          setToast(action === "stop" ? "出题已终止，已完成的问题已保留" : action === "retry" ? "已安排重试" : "测评已开放，可通知学生进入");
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
      const data = await api<ContestPreview>("/api/admin/quizzes/preview-contest", {
        method: "POST", body: JSON.stringify({ contest_id: Number(contestId), roster_text: rosterText }),
      });
      setPreview(data);
      setChoiceIds(data.problems.filter(p => p.include_choice).map(p => p.problem_id));
    } catch (error) {
      setMessage((error as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function updateRoster(file: File | null) {
    setBusy(true);
    setRosterError("");
    try {
      let text: string | null = null;
      if (file) {
        if (file.size > 65536) throw new Error("名单文件不能超过 64 KB");
        const bytes = new Uint8Array(await file.arrayBuffer());
        const encoding = bytes[0] === 0xff && bytes[1] === 0xfe ? "utf-16le" : bytes[0] === 0xfe && bytes[1] === 0xff ? "utf-16be" : "utf-8";
        try { text = new TextDecoder(encoding, { fatal: true }).decode(bytes); }
        catch { throw new Error("无法识别文本编码，请将名单保存为 UTF-8 纯文本后重试"); }
      }
      const updated = await api<ContestPreview>("/api/admin/quizzes/preview-contest", {
        method: "POST", body: JSON.stringify({ contest_id: Number(contestId), roster_text: text }),
      });
      setPreview(updated);
      setChoiceIds(updated.problems.filter(p => p.include_choice).map(p => p.problem_id));
      setRosterText(text);
      setRosterName(file?.name ?? "");
    } catch (error) { setRosterError((error as Error).message); }
    finally { setBusy(false); }
  }

  async function performCreateQuiz(allowBeforeCutoff: boolean) {
    if (lightweightEnabled) {
      const input = timeMode === "fixed" ? fixedMinutes : perQuestionMinutes;
      const duration = timeMode === "fixed" ? input : ((preview?.problems.length ?? 0) + choiceIds.length) * input;
      if (!Number.isInteger(input) || input < 1 || duration > 180) {
        setMessage("请输入正整数分钟，并确保总时长不超过 180 分钟。");
        return;
      }
    }
    setBusy(true);
    setMessage("");
    try {
      const data = await api<{ id: string; quiz_code: string }>("/api/admin/quizzes", {
        method: "POST",
        body: JSON.stringify({
          contest_id: Number(contestId), roster_text: rosterText, show_score_after_finish: false, allow_before_cutoff: allowBeforeCutoff,
          ...(lightweightEnabled ? { assessment_version: "lightweight_v1", choice_problem_ids: choiceIds,
            time_mode: timeMode,
            ...(timeMode === "fixed" ? { duration_minutes: fixedMinutes } : { minutes_per_question: perQuestionMinutes }) } : {}),
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
    if (busy || rosterError || !preview || (preview.roster && !preview.roster.matched_students.length)) return;
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

  function reopenQuiz(quiz: QuizSummary) {
    setConfirm({
      title: "重新开放这场测评？",
      description: "确认后，最后进入时间将设为当前时间的 30 分钟后。已有题目、作答和成绩会保留；已提交的学生不会重新开始作答。",
      confirmLabel: "重新开放",
      action: async () => {
        setBusy(true);
        setMessage("");
        try {
          await api(`/api/admin/quizzes/${quiz.id}/reopen`, { method: "POST" });
          await loadQuizzes();
          setToast("测评已重新开放，学生可在 30 分钟内进入");
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
      setAppeals(await api(`/api/admin/quizzes/${quiz.id}/appeals`));
      setView("results");
    } catch (error) {
      setMessage((error as Error).message);
    } finally {
      setBusy(false);
    }
  }

  function publishGrades() {
    if (!selected) return;
    const quiz = selected;
    setConfirm({ title: "公布本场成绩？",
      description: `涉及 ${results.length} 名学生。学生将看到本人作答、逐问得分、评分理由，以及参考答案和正确选项。公布后不得重新开放、重置作答或自动重评。`,
      confirmLabel: "确认公布", action: async () => {
        setBusy(true);
        try {
          await api(`/api/admin/quizzes/${quiz.id}/publish-scores`, {
            method: "POST", body: JSON.stringify({}),
          });
          setToast("成绩已公布");
          await loadQuizzes();
          await showResults({ ...quiz, scores_published: true });
        } catch (error) { setMessage((error as Error).message); }
        finally { setBusy(false); }
      } });
  }

  async function saveQuestionScore(questionId: string) {
    if (!attempt) return;
    setBusy(true);
    try {
      await api(`/api/admin/attempts/${attempt.id}/questions/${questionId}/score`, {
        method: "PATCH", body: JSON.stringify({ score: questionScore, reason: questionScoreReason,
          expected_version: attempt.score_version ?? 0, clear_override: false }),
      });
      setEditingScoreQuestion(null);
      await loadAttempt(attempt.id);
      setToast("逐题复核已保存");
    } catch (error) { setMessage((error as Error).message); }
    finally { setBusy(false); }
  }

  async function resolveSelectedAppeal() {
    const appeal = appeals.find(a => a.id === resolvingAppeal);
    if (!appeal || !selected) return;
    setBusy(true);
    setAppealError("");
    try {
      const detail = await api<AttemptDetail>(`/api/admin/attempts/${appeal.attempt_id}`);
      await api(`/api/admin/appeals/${appeal.id}/resolve`, {
        method: "POST", body: JSON.stringify({ expected_version: detail.score_version ?? 0,
          resolution: appealResolution, score: appealScore === "" ? null : Number(appealScore) }),
      });
      setResolvingAppeal(null);
      setAppealResolution("");
      setAppealScore("");
      await showResults(selected);
      setToast("申诉已处理");
    } catch (error) { setAppealError((error as Error).message); }
    finally { setBusy(false); }
  }

  async function loadPrepared(studentNumber: string) {
    if (!selected) return;
    setBusy(true);
    setMessage("");
    try {
      const detail = await api<PreparedDetail>(`/api/admin/quizzes/${selected.id}/students/${encodeURIComponent(studentNumber)}/prepared-questions`);
      setEditingQuestion(null);
      setPreparedDetail(detail);
      setView("preparation");
    } catch (error) { setMessage((error as Error).message); }
    finally { setBusy(false); }
  }

  async function savePreparedQuestion(event: FormEvent) {
    event.preventDefault();
    if (!selected || !preparedDetail || !editingQuestion) return;
    if (![editingQuestion.question, editingQuestion.question_en, editingQuestion.reference_answer].every(v => v?.trim())) { setEditError("请填写题干、英文对照和参考答案"); return; }
    if (selected.assessment_version !== "lightweight_v1" &&
      (editingQuestion.grading_points.length > 3 || editingQuestion.grading_points.some(v => !v.trim() || v.length > 5000))) {
      setEditError("请填写 1–3 条非空评分点，每条不超过 5000 字"); return;
    }
    if (selected.assessment_version === "lightweight_v1" &&
      (editingQuestion.response_format === "short_answer" ? !editingQuestion.core_idea?.trim() :
        editingQuestion.choices?.length !== 4 || !editingQuestion.correct_choice_id ||
        editingQuestion.choices.some(c => !c.text.trim() || !c.text_en.trim()))) {
      setEditError("请填写核心理解目标，或完整的四组选项与唯一正确选项"); return;
    }
    setBusy(true); setEditError("");
    try {
      await api(`/api/admin/quizzes/${selected.id}/students/${encodeURIComponent(preparedDetail.student_number)}/prepared-questions/${editingQuestion.job_id}/${editingQuestion.local_index}`, {
        method: "PATCH", body: JSON.stringify({expected_revision: editingQuestion.revision,
          question: editingQuestion.question, question_en: editingQuestion.question_en,
          reference_answer: editingQuestion.reference_answer,
          ...(selected.assessment_version === "lightweight_v1" ?
            editingQuestion.response_format === "short_answer"
              ? { core_idea: editingQuestion.core_idea }
              : { choices: editingQuestion.choices, correct_choice_id: editingQuestion.correct_choice_id }
            : { grading_points: editingQuestion.grading_points })}),
      });
      await loadPrepared(preparedDetail.student_number);
      setToast("题目已保存");
    } catch (error) { setEditError((error as Error).message); }
    finally { setBusy(false); }
  }

  function regeneratePreparedQuestions(problemId?: number) {
    if (!selected || !preparedDetail) return;
    const quiz = selected;
    const detail = preparedDetail;
    setConfirm({
      title: problemId ? `重新生成题目 ${problemId} 的问题？` : "重新生成这名学生的题目？",
      description: problemId ? `将按冻结配置重新生成 ${detail.student_number} 的题目 ${problemId}，产生模型调用费用。其他原题的问题保留。新题完成前，该学生暂不能开始测评。` : `将为 ${detail.student_number} 的 ${detail.preparation_total} 道有效题目重新出题，产生模型调用费用。新题全部完成前，该学生暂不能开始测评。其他学生不受影响。`,
      confirmLabel: "确认重新生成",
      action: async () => {
        setBusy(true);
        setMessage("");
        try {
          await api(`/api/admin/quizzes/${quiz.id}/students/${encodeURIComponent(detail.student_number)}/regenerate-questions`, {
            method: "POST", body: JSON.stringify({ expected_round: detail.round_no, problem_id: problemId }),
          });
          setPreparedDetail(null);
          await showResults(quiz);
          setToast("已安排重新出题，完成后可查看新题");
        } catch (error) { setMessage((error as Error).message); }
        finally { setBusy(false); }
      },
    });
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
      setToast("已加入评分队列，请稍后刷新查看结果");
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

      <main className={`admin-main ${view === "results" ? "admin-results" : ""}`}>
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
            <div><span><ClipboardList size={16} />测评总数</span><strong>{quizzes.length}</strong></div>
            <div><span><BookOpenCheck size={16} />当前开放</span><strong>{quizzes.filter(quiz => quiz.status.toUpperCase() === "PUBLISHED").length}</strong></div>
            <div><span><Users size={16} />学生总数</span><strong>{studentCount ?? "—"}</strong></div>
            <div><span><Check size={16} />已完成作答</span><strong>{totalFinished}</strong></div>
          </section>

          {quizzes.filter(quiz => quiz.preparation && (quiz.status.toUpperCase() === "DRAFT" || !quiz.preparation.ready)).map(quiz => {
            const p = quiz.preparation!;
            return <section className="card preparation-card" key={quiz.id} aria-label={`${quiz.name}出题进度`}>
              <div className="preparation-heading"><div><span className="eyebrow">课前准备</span><h2>{quiz.name}</h2></div><span className={`status-pill status-${p.ready ? "finished" : p.failed ? "grading-error" : "preparing"}`}>{p.ready ? "已就绪" : p.total === 0 ? "无有效提交" : p.cancelled && !p.running && !p.queued ? "已终止" : p.failed ? "部分出题失败" : "正在出题"}</span></div>
              <div className="preparation-counts"><div><strong>{p.completed}<small> / {p.total}</small></strong><span>已完成出题的有效题目</span></div><div><strong>{p.students_ready}<small> / {p.students_total}</small></strong><span>问题全部就绪的学生</span></div></div>
              <progress max={p.total || 1} value={p.completed} aria-label="已完成出题的有效题目" />
              <div className="preparation-footer"><span>{p.ready ? `已准备 ${p.completed_questions} 个问题，可以开放测评。` : p.total === 0 ? "本场没有可用于出题的提交，请核对 Contest 后重新创建。" : `出题中 ${p.running} · 等待 ${p.queued} · 失败 ${p.failed} · 已终止 ${p.cancelled} · 已准备 ${p.completed_questions} 问`}</span><div className="inline-actions">{p.running + p.queued > 0 && <button className="secondary" disabled={busy} onClick={() => preparationAction(quiz, "stop")}>终止出题</button>}{p.failed + p.cancelled > 0 && <button className="secondary" disabled={busy} onClick={() => preparationAction(quiz, "retry")}>重试未完成题目</button>}{quiz.status.toUpperCase() === "DRAFT" && <button disabled={busy || (!p.ready && !(p.students_ready > 0 && p.failed + p.cancelled > 0))} onClick={() => preparationAction(quiz)}>开放测评</button>}</div></div>
            </section>;
          })}

          <section className="card table-card quiz-list-card">
            <div className="quiz-list-heading"><div><h2>全部测评</h2><p>管理课堂测评，跟进学生作答。</p></div><span className="status-pill">{quizzes.length} 场测评</span></div>
            {quizzes.length ? <table className="quiz-table">
              <thead><tr><th>测评</th><th>Contest</th><th>最后进入时间</th><th>完成进度</th><th>平均原始分</th><th>操作</th></tr></thead>
              <tbody>{quizzes.map(quiz => {
                const progress = quiz.participant_count ? Math.min(100, quiz.finished_count / quiz.participant_count * 100) : 0;
                return <tr key={quiz.id}>
                  <td><div className="quiz-name-cell"><span className={`status-dot dot-${statusClass(quiz.status)}`} /><div><strong>{quiz.name}</strong><small>{statusLabel(quiz.status)}</small></div></div></td>
                  <td className="mono">#{quiz.uoj_contest_id}</td>
                  <td>{quiz.status.toUpperCase() === "DRAFT" ? "开放后 30 分钟" : formatDate(quiz.end_time)}</td>
                  <td><div className="table-progress"><span><strong>{quiz.finished_count}</strong> / {quiz.participant_count}</span><i><b style={{ width: `${progress}%` }} /></i></div></td>
                  <td><strong>{quiz.average_score !== null ? `${quiz.average_score.toFixed(1)} 分` : "—"}</strong></td>
                  <td className="actions-cell"><div className="actions"><button className="link primary-link" onClick={() => void showResults(quiz)}>查看结果</button><details className="action-menu" name="quiz-actions"><summary aria-label="更多操作"><MoreHorizontal size={18} /></summary><div onClick={event => { const menu = event.currentTarget.closest("details"); if (menu) menu.open = false; }}><button onClick={() => void copyText(studentLink(quiz.id), "学生链接已复制")}><Copy size={15} />复制学生链接</button><button disabled={busy} onClick={() => regenerateCode(quiz)}><RefreshCw size={15} />生成备用码</button>{["CLOSED", "ACTIVE"].includes(quiz.status.toUpperCase()) && <button disabled={busy} onClick={() => reopenQuiz(quiz)}><RotateCcw size={15} />重新开放测评</button>}<button className="delete-quiz" disabled={busy} onClick={() => deleteQuiz(quiz)}><Trash2 size={15} />删除此测评</button></div></details></div></td>
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
              <div className="section-body"><div className="step-label">第一步</div><h2>导入比赛</h2><p className="muted">输入 Contest ID，导入本次测评的比赛信息。</p><label>Contest ID<input type="number" min="1" disabled={busy} value={contestId} onChange={event => { setContestId(event.target.value); setPreview(null); setRosterText(null); setRosterName(""); setRosterError(""); setCreatedCode(""); }} placeholder="例如：1" required /><small>即 UOJ 比赛页面网址末尾的数字</small></label><button disabled={busy}>{busy && <LoaderCircle className="spin" size={16} />}{busy ? "正在导入" : preview ? "重新导入" : "导入并预览"}</button></div>
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
                  {lightweightEnabled && <AssessmentSetup problems={preview.problems} choiceIds={choiceIds} onChoices={setChoiceIds}
                    timeMode={timeMode} onTimeMode={setTimeMode} perQuestionMinutes={perQuestionMinutes}
                    onPerQuestionMinutes={setPerQuestionMinutes} fixedMinutes={fixedMinutes} onFixedMinutes={setFixedMinutes} />}
                  {!lightweightEnabled && <p className="muted">新版自由作答协议仍在质量回归前关闭创建入口；当前创建沿用历史测评规则。</p>}
                  {preview.parser_errors.length > 0 && <details className="parser-errors"><summary>查看 {preview.parser_errors.length} 条导入异常</summary><ul>{preview.parser_errors.map((error, index) => <li key={`${error.student_number}-${error.problem_id}-${index}`}>{error.student_number} · 题目 {error.problem_id}：{error.error}</li>)}</ul></details>}
                  <section className="roster-panel" aria-label="参与范围">
                    <div className="roster-heading"><div><h3><Users size={18} />参与范围 <span>可选</span></h3><p>{rosterText === null ? "全部可参与学生" : "按名单抽查"}</p></div>
                      <label className={`button secondary roster-upload ${busy ? "disabled" : ""}`}><FileText size={16} />{rosterName ? "更换名单" : "上传抽查名单"}<input type="file" aria-label="上传抽查名单" disabled={busy} onChange={event => { const file = event.target.files?.[0]; event.target.value = ""; if (file) void updateRoster(file); }} /></label>
                    </div>
                    <p className="roster-hint">仅抽查部分学生时上传名单；不上传则选择全部可参与学生。</p>
                    <details className="roster-format"><summary>名单怎么写？</summary><p>每行一个学号（用户名），也可用空格、逗号分隔。无需姓名、标题或序号，重复学号自动合并。</p><pre>{"231250001\n231250002\n231250003"}</pre><p>支持 .txt、.md 或任意后缀的纯文本文件，最大 64 KB。建议使用 UTF-8 编码。</p></details>
                    {(rosterName || rosterError) && <div className="roster-file"><FileText size={16} /><span>{rosterName || "名单未导入"}</span><button className="text-button" disabled={busy} onClick={() => void updateRoster(null)}>移除名单，改为全员</button></div>}
                    {busy && <p className="roster-hint" role="status">正在核对参与名单…</p>}
                    {rosterError && <p className="roster-error" role="alert">{rosterError}</p>}
                    {!rosterError && preview.roster && <div className="roster-report" aria-live="polite">
                      <div className="roster-summary"><strong>{preview.roster.matched_students.length}<small>人将参加测评</small></strong><span>名单共 {preview.roster.requested_count} 人{preview.roster.duplicate_count > 0 ? `，已合并 ${preview.roster.duplicate_count} 条重复记录` : ""}</span></div>
                      {preview.roster.matched_students.length === 0 && <p className="roster-error">没有匹配到可参与学生，请调整名单后再创建。</p>}
                      {[["查看入选名单", preview.roster.matched_students], ["不在本场学生列表", preview.roster.unknown_students], ["没有有效提交", preview.roster.unavailable_students]].map(([label, names]) => {
                        const students = names as string[];
                        return students.length > 0 && <details key={label as string} className="roster-names"><summary>{label as string} · {students.length} 人</summary><div>{students.map(name => <code key={name}>{name}</code>)}</div></details>;
                      })}
                    </div>}
                  </section>
                  <div className="policy-box"><div><Check size={16} /><span>创建后开始出题</span></div><div><Check size={16} /><span>教师开放后 30 分钟内进入</span></div><div><Check size={16} /><span>{lightweightEnabled ? "按原题冻结一问或两问" : "每道有效题目固定 2 问"}</span></div><div><Check size={16} /><span>{lightweightEnabled ? "整份自由分配时间" : "每个问题 3 分钟"}</span></div></div>
                  <p className="muted">将为 {preview.roster?.matched_students.length ?? preview.students_with_eligible_problem} 名学生的 {preview.roster?.selected_submission_snapshots ?? preview.selected_submission_snapshots} 道有效题目准备问题，产生相应的模型调用费用。出题完成后，由你决定何时开放。</p>
                  <button className="create-submit" onClick={createQuiz} disabled={busy || !!rosterError || (preview.roster !== null && !!preview.roster && preview.roster.matched_students.length === 0)}>{busy && <LoaderCircle className="spin" size={16} />}{busy ? "正在创建" : preview.cutoff_reached ? "创建并准备问题" : "提前创建并准备问题"}</button>
                  {createdCode && selected && <div className="code-reveal"><div className="reveal-symbol"><Check size={18} /></div><div><span>测评已创建</span><small>复制学生链接即可发布；备用码只需在登录异常时提供。</small></div><div className="inline-actions"><button onClick={() => void copyText(studentLink(selected.id), "学生链接已复制")}><Copy size={16} />复制学生链接</button><details><summary>备用码</summary><strong>{createdCode}</strong><button className="secondary" onClick={() => void copyText(createdCode, "备用测评码已复制")}>复制</button></details></div></div>}
                </>}
              </div>
            </section>
          </div>
        </>}

        {view === "results" && selected && <>
          <header className="results-page-header">
            <div><button className="back" onClick={() => setView("list")}><ArrowLeft size={15} />返回测评管理</button><div className="results-title"><h1>{selected.name}</h1><span className={`status-pill ${selected.scores_published ? "status-finished" : "status-ready"}`}>{selected.scores_published ? "成绩已公布" : "成绩未公布"}</span></div><p><span className="live-dot" />结果与申诉每 10 秒自动刷新</p></div>
            <div className="results-publish-actions"><div className="header-actions"><a className="button secondary" href={`/api/admin/quizzes/${selected.id}/export`}><Download size={16} />导出 CSV</a>
              <button type="button" onClick={publishGrades} disabled={busy || selected.scores_published}>
                {selected.scores_published ? <Check size={16} /> : <BookOpenCheck size={16} />}{selected.scores_published ? "已公布成绩" : "公布成绩"}</button></div><p>{selected.scores_published ? "学生可查看成绩、参考答案与正确选项" : "公布成绩时，将同时公布参考答案与正确选项"}</p></div>
          </header>
          <section className="result-overview">
            <div><Users size={18} /><span>学生总数<strong>{results.length}</strong></span></div>
            <div><Check size={18} /><span>已完成<strong>{results.filter(result => !result.review_required && result.attempt_status?.toUpperCase() === "FINISHED").length}</strong></span></div>
            <div><BarChart3 size={18} /><span>平均原始分<strong>{(() => { const values = results.map(confirmedResultScore).filter((score): score is number => score !== null); return values.length ? `${(values.reduce((sum, value) => sum + value, 0) / values.length).toFixed(1)} 分` : "—"; })()}</strong></span></div>
            <div><CircleAlert size={18} /><span>需关注学生<strong>{results.filter(result => resultBucket(result) === "attention").length}</strong></span></div>
          </section>
          <AppealPanel appeals={appeals} studentFor={studentForAttempt} busy={busy}
            onReview={appeal => { setResolvingAppeal(appeal.id); setAppealResolution(""); setAppealScore(""); setAppealError(""); }}
            onViewAttempt={id => void loadAttempt(id)} />
          <section className="card table-card result-card">
            <div className="results-table-heading"><h2>学生成绩</h2><span>按学生查看作答进度与评分结果</span></div>
            {results.length ? <><div className="result-toolbar"><label><Search size={15} /><input aria-label="搜索学号" value={resultQuery} onChange={event => setResultQuery(event.target.value)} placeholder="搜索学号" /></label><div className="filter-tabs" role="group" aria-label="筛选作答状态"><button className={resultFilter === "all" ? "active" : ""} onClick={() => setResultFilter("all")}>全部</button><button className={resultFilter === "finished" ? "active" : ""} onClick={() => setResultFilter("finished")}>已完成</button><button className={resultFilter === "active" ? "active" : ""} onClick={() => setResultFilter("active")}>进行中</button><button className={resultFilter === "pending" ? "active" : ""} onClick={() => setResultFilter("pending")}>未作答</button><button className={resultFilter === "attention" ? "active" : ""} onClick={() => setResultFilter("attention")}>需关注</button></div><span>{filteredResults.length} / {results.length} 人</span></div>
              {filteredResults.length ? <table className="result-table">
                <thead><tr><th>学号</th><th>覆盖题目</th><th>问题数</th><th>原始分</th><th>等级</th><th>最低置信度</th><th>状态</th><th></th></tr></thead>
                <tbody>{filteredResults.map(result => <tr key={result.student_number}>
                  <td className="mono student-number">{result.student_number}</td><td>{result.problem_count}</td><td>{result.question_count}</td>
                  <td><strong>{(result.review_required ? result.auto_score : result.final_score) ?? "—"}{result.max_score ? ` / ${result.max_score}` : ""}</strong>{result.review_required && <small>建议分 · 待复核</small>}{result.manual_score !== null && <small>人工覆盖</small>}</td>
                  <td><strong className="grade-score">{scoreGrade(confirmedResultScore(result))}</strong></td>
                  <td>{result.confidence?.toFixed(2) ?? "—"}</td>
                  <td><span className={`status-pill status-${statusClass(result.review_required ? "GRADING_ERROR" : result.attempt_status || result.participant_status)}`}>{statusLabel(result.review_required ? "REVIEW_REQUIRED" : result.attempt_status || result.participant_status)}</span>{result.quality_attention && <span className="status-pill quality-attention">题目质量需关注</span>}{result.timed_out && <small>超时自动交卷</small>}</td>
                  <td>{result.attempt_id && <button className="link" disabled={busy} onClick={() => void loadAttempt(result.attempt_id!)}>查看详情</button>}{(!result.attempt_id || result.attempt_status === "RESET") && result.prepared_problem_count > 0 && <button className="link" disabled={busy} onClick={() => void loadPrepared(result.student_number)}>{result.attempt_id ? "查看新题" : "查看详情"}</button>}</td>
                </tr>)}</tbody></table> : <div className="filtered-empty">没有符合当前条件的学生</div>}</> : <div className="empty-state compact-empty"><div className="empty-symbol"><Users size={25} /></div><h2>暂时没有作答记录</h2><p>学生进入并开始测评后，进度会自动显示在这里。</p></div>}
          </section>
        </>}

        {view === "preparation" && preparedDetail && <>
          <header><div><button className="back" onClick={() => setView("results")}><ArrowLeft size={15} />返回测评结果</button><div className="eyebrow">QUESTION PREVIEW</div><h1 className="mono">{preparedDetail.student_number}</h1><p>已准备 {preparedDetail.prepared_problem_count} / {preparedDetail.preparation_total} 道题目，共 {preparedDetail.questions.length} 个问题</p></div><div className="inline-actions"><span className="status-pill status-ready">预生成题目</span><button className="secondary" disabled={busy} onClick={() => regeneratePreparedQuestions()}><RefreshCw size={16} />重新生成题目</button></div></header>
          <div className="problem-review-list">{groupAttemptQuestions(preparedDetail.questions).map(group => <section className="card problem-review" key={group.problem.id}>
            <header><div><span className="problem-number">题目 {group.problem.id}</span><h2>{group.problem.title}</h2></div><div className="inline-actions"><span className="question-count">{group.questions.length} 个问题</span>{preparedDetail.can_edit && <button className="secondary" disabled={busy} onClick={() => regeneratePreparedQuestions(group.problem.id)}><RefreshCw size={15} />重新生成这道题</button>}</div></header>
            <details className="material-details"><summary><FileText size={15} />查看题面与提交代码</summary><div className="review-material"><ProblemStatement className="statement" text={group.problem.statement} /><div className="source-card"><CodeBlock code={group.sourceCode} language={group.language} /></div></div></details>
            <div className="question-list">{group.questions.map((question, index) => <article className="question-detail" key={question.index}>
              <div className="question-title-row"><div className="question-index">问题 {index + 1}</div>{preparedDetail.can_edit && <button className="link" disabled={busy} onClick={() => { setEditingQuestion({...question, grading_points: [...question.grading_points]}); setEditError(""); }}>编辑问题</button>}</div><ProblemStatement className="generated-question" text={question.question} />
               {qualityAuditEnabled && <QualityAudit review={question.quality} />}
               {question.choices && <ol className="review-options">{question.choices.map(choice => <li key={choice.id}><strong>{choice.id}</strong><ProblemStatement text={choice.text} /></li>)}</ol>}
              {question.question_en && <ProblemStatement className="question-en" text={question.question_en} />}
               <dl><dt>参考答案</dt><dd>{question.reference_answer}</dd>
                 {question.core_idea && <><dt>核心理解目标</dt><dd>{question.core_idea}</dd></>}
                 {question.correct_choice_id && <><dt>正确选项</dt><dd>{question.correct_choice_id}</dd></>}
                 {question.grading_points.length > 0 && <><dt>出题评分点</dt><dd>{question.grading_points.join("；")}</dd></>}</dl>
            </article>)}</div>
          </section>)}</div>
        </>}

        {view === "attempt" && attempt && <>
          <header>
            <div><button className="back" onClick={() => setView("results")}><ArrowLeft size={15} />返回测评结果</button><div className="eyebrow">STUDENT REVIEW</div><h1 className="mono">{attempt.student_number}</h1><p>共 {attemptGroups.length} 道题、{attempt.questions.length} 个问题 · <span className={`status-pill status-${statusClass(attempt.review_required ? "GRADING_ERROR" : attempt.status)}`}>{statusLabel(attempt.review_required ? "REVIEW_REQUIRED" : attempt.status)}</span></p>{attempt.completed_at && <p>完成时间：<time dateTime={attempt.completed_at}>{new Date(attempt.completed_at).toLocaleString("zh-CN", { hour12: false })}</time>{attempt.timed_out && "（到时自动交卷）"}</p>}</div>
            {!selected?.scores_published && <div className="header-actions"><button className="secondary" disabled={busy} onClick={regrade}><RefreshCw size={16} />重新评分</button><button className="danger" disabled={busy} onClick={resetAttempt}><RotateCcw size={16} />允许重新作答</button></div>}
          </header>
          <section className="detail-grid">
            <div className="problem-review-list">{attemptGroups.map(group => <section className="card problem-review" key={group.problem.id}>
              <header><div><span className="problem-number">题目 {group.problem.id}</span><h2>{group.problem.title}</h2></div><span className="question-count">{group.questions.length} 个问题</span></header>
              <details className="material-details"><summary><FileText size={15} />查看题面与提交代码</summary><div className="review-material"><ProblemStatement className="statement" text={group.problem.statement} /><div className="source-card"><CodeBlock code={group.sourceCode} language={group.language} /></div></div></details>
              <div className="question-list">{group.questions.map((question, index) => <article className="question-detail" key={question.index}>
                <div className="question-title-row"><div className="question-index">问题 {index + 1}</div><span className="question-score">{attempt.review_required ? "建议 " : ""}{question.score ?? "—"} / 2</span></div>
                 <ProblemStatement className="generated-question" text={question.question} />
                 {question.choices && <ol className="review-options">{question.choices.map(choice => <li key={choice.id}><strong>{choice.id}</strong><ProblemStatement text={choice.text} /></li>)}</ol>}
                {question.review_required && <p className="review-notice">{attempt.review_required ? "待教师复核" : "曾触发复核"}：{readableReviewReason(question.review_reason)}</p>}
                 {qualityAuditEnabled && <QualityAudit review={question.quality} />}
              {question.question_en && <ProblemStatement className="question-en" text={question.question_en} />}
                 <dl><dt>学生回答</dt><dd className="student-answer">{question.choice_id || question.student_answer || "尚未回答"}</dd>
                   <dt>评分原因</dt><dd>{question.reason ?? "—"}</dd>
                   <dt>有效分</dt><dd>{question.effective_score ?? question.score ?? "—"}</dd>
                   <dt>置信度</dt><dd>{question.confidence?.toFixed(2) ?? "—"}</dd>
                   <dt>参考答案</dt><dd>{question.reference_answer}</dd>
                   {question.correct_choice_id && <><dt>正确选项</dt><dd>{question.correct_choice_id}</dd></>}
                   {question.student_dispute && <><dt>学生异议</dt><dd>{question.dispute_reason || "学生标记题目有疑问"}</dd></>}</dl>
                 {attempt.assessment_version === "lightweight_v1" && <div className="question-score-editor">
                    <button type="button" className="secondary" onClick={() => { setEditingScoreQuestion(question.id || null);
                     setQuestionScore(question.effective_score ?? question.score ?? 0); setQuestionScoreReason(""); }}>
                     {question.review_required ? "处理复核" : "逐题改分"}</button>
                   {editingScoreQuestion === question.id && <div>
                     <label>有效分<input type="number" min="0" max="2" value={questionScore}
                       onChange={event => setQuestionScore(Number(event.target.value))} /></label>
                     <label>处理说明<textarea value={questionScoreReason}
                       onChange={event => setQuestionScoreReason(event.target.value)} maxLength={5000} /></label>
                     <button type="button" disabled={busy || !questionScoreReason.trim()}
                       onClick={() => void saveQuestionScore(question.id!)}>确认并保存</button>
                   </div>}
                 </div>}
              </article>)}</div>
            </section>)}</div>
            {attempt.assessment_version === "lightweight_v1" ?
              <div className="card override-card"><h2>逐题成绩</h2><p>逐题复核后，总分按有效分相加。机器原始分与理由保留。</p>
                <div className="score compact">{attempt.questions.reduce((sum, q) => sum + (q.effective_score ?? 0), 0)}<small> / {attempt.max_score}</small></div></div> :
              <div className="card override-card"><div className="override-heading"><h2>成绩复核</h2><ShieldCheck size={19} /></div>{attempt.review_required && <p className="review-notice">本次评分待复核，建议分尚未计入最终成绩。请检查题目和评分依据，填写总分与原因后保存。</p>}<div className="score compact">{(attempt.review_required ? attempt.auto_score : attempt.manual_override_score ?? attempt.auto_score) ?? "—"}<small> / {attempt.max_score}</small></div><div className="percent-large">{attempt.final_percent !== null ? `${attempt.final_percent.toFixed(1)}%` : "—"}</div><div className="score-meta"><span>{attempt.review_required ? "待复核建议分" : "当前百分制"}</span><span>自动总分 {attempt.auto_score ?? "—"}</span></div><form onSubmit={saveOverride}><label>人工覆盖分数<input type="number" min="0" max={attempt.max_score} value={overrideScore} onChange={event => setOverrideScore(Number(event.target.value))} /></label><label>覆盖原因<textarea required maxLength={5000} value={overrideReason} onChange={event => setOverrideReason(event.target.value)} placeholder="请填写复核依据" /></label><button disabled={busy}>{busy && <LoaderCircle className="spin" size={16} />}保存复核结果</button></form></div>}
          </section>
        </>}
      </main>

      {editingQuestion && <div className="question-edit-overlay"><form className="card question-edit-dialog" onSubmit={savePreparedQuestion} role="dialog" aria-modal="true" aria-labelledby="question-edit-title">
        <h2 id="question-edit-title">编辑问题</h2><p className="muted">支持 Markdown 与公式。请同步核对英文对照与答案。</p>
        <label>中文题干<textarea required maxLength={500} value={editingQuestion.question} onChange={e => setEditingQuestion({...editingQuestion, question:e.target.value})} /></label>
        <label>英文对照<textarea required maxLength={800} value={editingQuestion.question_en || ""} onChange={e => setEditingQuestion({...editingQuestion, question_en:e.target.value})} /></label>
        <details><summary>预览题干</summary><ProblemStatement text={editingQuestion.question} /><ProblemStatement className="question-en" text={editingQuestion.question_en || ""} /></details>
        <label>参考答案<textarea required maxLength={5000} value={editingQuestion.reference_answer} onChange={e => setEditingQuestion({...editingQuestion, reference_answer:e.target.value})} /></label>
        {selected?.assessment_version === "lightweight_v1" ?
          editingQuestion.response_format === "short_answer" ?
            <label>核心理解目标<textarea required maxLength={1000} value={editingQuestion.core_idea || ""}
              onChange={e => setEditingQuestion({...editingQuestion, core_idea:e.target.value})} /></label> :
            <div className="edit-choices">
              {editingQuestion.choices?.map((choice, i) => <div key={choice.id}>
                <strong>{choice.id}</strong>
                <label>中文选项<textarea required value={choice.text} onChange={e => setEditingQuestion({
                  ...editingQuestion, choices: editingQuestion.choices?.map((old, j) =>
                    j === i ? { ...old, text: e.target.value } : old),
                })} /></label>
                <label>英文选项<textarea required value={choice.text_en} onChange={e => setEditingQuestion({
                  ...editingQuestion, choices: editingQuestion.choices?.map((old, j) =>
                    j === i ? { ...old, text_en: e.target.value } : old),
                })} /></label>
                <ProblemStatement text={choice.text} />
              </div>)}
              <label>正确选项<select value={editingQuestion.correct_choice_id || ""}
                onChange={e => setEditingQuestion({...editingQuestion, correct_choice_id:e.target.value})}>
                <option value="">请选择</option>{editingQuestion.choices?.map(c => <option key={c.id} value={c.id}>{c.id}</option>)}
              </select></label>
            </div>
          : <label>评分点（每行一条，1–3 条）<textarea required maxLength={15002} value={editingQuestion.grading_points.join("\n")} onChange={e => setEditingQuestion({...editingQuestion, grading_points:e.target.value.split("\n")})} /></label>}
        {qualityAuditEnabled && <p className="muted">保存后原质量审核结论失效，可按需重新审核。</p>}
        {editError && <div className="error" role="alert">{editError}</div>}
        <div className="inline-actions"><button type="button" className="secondary" disabled={busy} onClick={() => setEditingQuestion(null)}>取消</button><button disabled={busy}>{busy ? "正在保存…" : "保存修改"}</button></div>
      </form></div>}
      {activeAppeal && <AppealReviewDialog appeal={activeAppeal} student={studentForAttempt(activeAppeal.attempt_id)}
        resolution={appealResolution} score={appealScore} busy={busy} error={appealError} onResolution={setAppealResolution}
        onScore={setAppealScore} onCancel={() => setResolvingAppeal(null)} onSubmit={() => void resolveSelectedAppeal()} />}
      {toast && <div className="toast" role="status"><Check size={17} />{toast}</div>}
      {confirm && <ConfirmDialog title={confirm.title} description={confirm.description} confirmLabel={confirm.confirmLabel} danger={confirm.danger} busy={busy} onCancel={() => setConfirm(null)} onConfirm={() => void runConfirmedAction()} />}
    </div>
  );
}
