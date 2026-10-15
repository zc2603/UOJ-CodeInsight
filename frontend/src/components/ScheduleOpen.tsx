import { useState } from "react";
import { createPortal } from "react-dom";
import { Clock } from "lucide-react";
import { api } from "../api";
import type { QuizSummary } from "../types";
import { ConfirmDialog } from "./ConfirmDialog";

export function beijingTime(value: string) {
  return new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", year: "numeric",
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date(value));
}

export function ScheduleOpen({ quiz, disabled, onSaved }: {
  quiz: QuizSummary; disabled: boolean; onSaved: () => Promise<unknown>;
}) {
  const [mode, setMode] = useState<"edit" | "cancel" | null>(null);
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const date = value ? new Date(`${value}:00+08:00`) : null;
  const valid = date !== null && Number.isFinite(date.getTime());
  async function save() {
    if (mode === "edit" && (!valid || date!.getTime() <= Date.now())) {
      setError("请选择未来的开放时间"); return;
    }
    setBusy(true); setError("");
    try {
      await api(`/api/admin/quizzes/${quiz.id}/schedule`, { method: "PUT",
        body: JSON.stringify({ scheduled_open_at: mode === "cancel" ? null : date!.toISOString() }) });
      await onSaved(); setMode(null);
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  return <>
    <button className="secondary" disabled={disabled || busy} onClick={() => {
      const time = quiz.scheduled_open_at ? new Date(quiz.scheduled_open_at).getTime() : Date.now() + 3600000;
      setValue(new Date(time + 8 * 3600000).toISOString().slice(0, 16));
      setError(""); setMode("edit");
    }}><Clock size={16} />{quiz.scheduled_open_at ? "修改时间" : "定时开放"}</button>
    {quiz.scheduled_open_at && <button className="secondary" disabled={disabled || busy}
      onClick={() => { setError(""); setMode("cancel"); }}>取消定时</button>}
    {mode && createPortal(<ConfirmDialog title={mode === "cancel" ? "取消定时开放？" : "设置定时开放"}
      description={mode === "cancel" ? "取消后，测评保持未开放状态。" : "到点时须全部题目就绪，否则标记失败，由你处理。关闭网页不影响预约。"}
      confirmLabel={mode === "cancel" ? "确认取消" : "确认定时开放"} busy={busy}
      onCancel={() => setMode(null)} onConfirm={() => void save()}>
      {mode === "edit" && <>
        <label>开放时间（北京时间）<input type="datetime-local" value={value} disabled={busy}
          onChange={event => setValue(event.target.value)} /></label>
        <p>进入窗口：{quiz.entry_minutes ?? 30} 分钟</p>
        {valid && <p>最后进入时间：{beijingTime(new Date(date!.getTime() + (quiz.entry_minutes ?? 30) * 60000).toISOString())}（北京时间）</p>}
        <p className="muted">每位学生从开始作答时独立计时。</p>
      </>}
      {error && <p className="error" role="alert">{error}</p>}
    </ConfirmDialog>, document.body)}
  </>;
}
