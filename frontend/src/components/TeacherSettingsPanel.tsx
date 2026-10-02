import { useState } from "react";
import { api } from "../api";
import { CodeBlock } from "./CodeBlock";
import { columnLabels, type TeacherSettings, type SettingsResponse } from "../teacherSettings";
import { RuntimeSettingsPanel } from "./RuntimeSettingsPanel";

const groups = {
  assessment: ["entry_minutes", "reopen_minutes", "time_mode", "minutes_per_question", "fixed_minutes", "question_template"],
  grades: ["grade_bands"],
  display: ["result_filter", "result_sort", "result_columns", "code_font_size", "code_wrap", "english_expanded"],
  appeals: ["appeal_window_days", "appeal_prompt"],
} satisfies Record<string, (keyof TeacherSettings)[]>;

export function TeacherSettingsPanel({ value, defaults, revision, onSaved, onReload }: {
  value: TeacherSettings; defaults: TeacherSettings; revision: number;
  onSaved: (response: SettingsResponse) => void; onReload: () => Promise<void>;
}) {
  const [draft, setDraft] = useState<TeacherSettings>(structuredClone(value));
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [previewScore, setPreviewScore] = useState(6);
  function change<K extends keyof TeacherSettings>(key: K, val: TeacherSettings[K]) {
    setDraft(old => ({ ...old, [key]: val })); setMessage(""); setError("");
  }
  function reset(group: keyof typeof groups) {
    const restored = Object.fromEntries(groups[group].map(key => [key, structuredClone(defaults[key])]));
    setDraft(old => ({ ...old, ...restored })); setMessage("本组已恢复初始值，点击保存后生效。");
  }
  async function save(group: keyof typeof groups) {
    const settings = { ...value, ...Object.fromEntries(groups[group].map(key => [key, draft[key]])) };
    setBusy(true); setError(""); setMessage("");
    try {
      const saved = await api<SettingsResponse>("/api/admin/settings", { method: "PUT",
        body: JSON.stringify({ expected_revision: revision, settings }) });
      onSaved(saved); setMessage("本组设置已保存。");
    } catch (caught) { setError((caught as Error).message); }
    finally { setBusy(false); }
  }
  function actions(group: keyof typeof groups) {
    return <div className="settings-actions"><button type="button" disabled={busy} onClick={() => void save(group)}>保存本组</button>
      <button type="button" className="secondary" disabled={busy} onClick={() => reset(group)}>恢复本组默认值</button></div>;
  }
  const number = (key: "entry_minutes" | "minutes_per_question" | "fixed_minutes", label: string, max: number) =>
    <label>{label}<input type="number" min={1} max={max} step={1} value={draft[key] || ""}
      onChange={e => change(key, Number(e.target.value))} /></label>;
  return <>
    <header><div><div className="eyebrow">TEACHER SETTINGS</div><h1>设置</h1><p>保存常用测评规则和个人阅读偏好，跨设备使用。</p></div>
      <button className="secondary" disabled={busy} onClick={() => { void onReload().catch(e => setError((e as Error).message)); }}>重新加载</button></header>
    {error && <div className="error" role="alert">{error}</div>}
    {message && <div className="notice" role="status">{message}</div>}
    <div className="settings-layout">
      <section className="card settings-section"><h2>测评默认值</h2><p className="muted">用于以后新建的测评；创建时仍可单独调整。本场规则在创建时保存。</p>
        <div className="settings-grid">{number("entry_minutes", "首次开放进入窗口（分钟）", 1440)}
          <label>重新开放进入窗口<select value={draft.reopen_minutes === null ? "follow" : "custom"}
            onChange={e => change("reopen_minutes", e.target.value === "follow" ? null : draft.entry_minutes)}>
            <option value="follow">跟随首次开放</option><option value="custom">单独设置</option></select></label>
          {draft.reopen_minutes !== null && <label>重新开放分钟数<input type="number" min={1} max={1440} value={draft.reopen_minutes || ""}
            onChange={e => change("reopen_minutes", Number(e.target.value))} /></label>}
        </div>
        <div className="inline-actions">{[15, 30, 45, 60].map(n => <button className="secondary" key={n} onClick={() => change("entry_minutes", n)}>{n} 分钟</button>)}</div>
        <div className="settings-grid"><label>个人时长计算<select value={draft.time_mode} onChange={e => change("time_mode", e.target.value as TeacherSettings["time_mode"])}>
          <option value="per_question">按实际问题数</option><option value="fixed">固定总时长</option></select></label>
          {number("minutes_per_question", "每问折算分钟数", 180)}{number("fixed_minutes", "固定总时长预填值（分钟）", 180)}
          <label>原题默认安排<select value={draft.question_template} onChange={e => change("question_template", e.target.value as TeacherSettings["question_template"])}>
            <option value="standard">默认：最后一题仅简答</option><option value="all_choice">全部简答＋单选</option><option value="all_short">全部仅简答</option></select><small>默认方案：三题及以上时最大 ID 原题仅简答，其余简答＋单选；不足三题时均为简答＋单选。</small></label>
        </div><p className="muted">进入窗口决定何时可以开始；个人时长从学生开始时独立计算，总时长不超过 180 分钟。</p>{actions("assessment")}
      </section>
      <section className="card settings-section"><h2>成绩等级</h2><p className="muted">按原始得分划分，保存到新测评。最低分从高到低填写，末级为 0。</p>
        <div className="grade-settings">{draft.grade_bands.map((band, i) => <div key={i}>
          <label>等级名称<input value={band.label} maxLength={12} onChange={e => change("grade_bands", draft.grade_bands.map((b, j) => i === j ? { ...b, label: e.target.value } : b))} /></label>
          <label>最低原始分<input type="number" min={0} max={400} value={band.minimum} onChange={e => change("grade_bands", draft.grade_bands.map((b, j) => i === j ? { ...b, minimum: Number(e.target.value) } : b))} /></label>
        </div>)}</div>
        <label>预览原始得分<input type="number" min={0} value={previewScore} onChange={e => setPreviewScore(Number(e.target.value))} /></label>
        <p>等级预览：<strong>{draft.grade_bands.find(b => previewScore >= b.minimum)?.label || "—"}</strong> · 按原始分判断，与实际满分无关。</p>{actions("grades")}
      </section>
      <section className="card settings-section"><h2>成绩表与阅读偏好</h2><p className="muted">保存后应用到你的教师界面。</p>
        <div className="settings-grid"><label>默认筛选<select value={draft.result_filter} onChange={e => change("result_filter", e.target.value as TeacherSettings["result_filter"])}>
          {Object.entries({ all: "全部", attention: "需关注", pending: "未作答", active: "进行中", finished: "已完成" }).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></label>
          <label>默认排序<select value={draft.result_sort} onChange={e => change("result_sort", e.target.value as TeacherSettings["result_sort"])}>
            <option value="student">学号升序</option><option value="status">需关注优先</option><option value="completed">最近完成优先</option><option value="score">成绩从高到低</option></select></label>
        </div>
        <fieldset><legend>显示列</legend><div className="settings-checks">{(Object.keys(columnLabels) as TeacherSettings["result_columns"]).map(key => <label key={key}>
          <input type="checkbox" checked={draft.result_columns.includes(key)} onChange={e => change("result_columns", e.target.checked ? [...draft.result_columns, key] : draft.result_columns.filter(k => k !== key))} />{columnLabels[key]}</label>)}</div></fieldset>
        <div className="settings-grid"><label>代码字号<input type="number" min={11} max={22} value={draft.code_font_size} onChange={e => change("code_font_size", Number(e.target.value))} /></label></div>
        <div className="settings-checks"><label><input type="checkbox" checked={draft.code_wrap} onChange={e => change("code_wrap", e.target.checked)} />代码自动换行</label>
          <label><input type="checkbox" checked={draft.english_expanded} onChange={e => change("english_expanded", e.target.checked)} />英文对照默认展开</label></div>
        <div className="settings-code-preview"><CodeBlock code={'// 代码显示预览\nfor (int index = 0; index < numbers.size(); ++index) {\n    result += numbers[index]; // 自动换行仍保留源码行号\n}'} fontSize={draft.code_font_size} wrap={draft.code_wrap} /></div>
        {actions("display")}
      </section>
      <section className="card settings-section"><h2>申诉默认规则</h2><p className="muted">用于新测评，从首次公布成绩时开始计算；已提交的申请可继续由教师处理。</p>
        <div className="settings-grid"><label>默认申诉期限<select aria-label="默认申诉期限" value={draft.appeal_window_days == null ? "unlimited" : "days"}
          onChange={e => change("appeal_window_days", e.target.value === "unlimited" ? null : 7)}>
          <option value="unlimited">不限期</option><option value="days">公布后指定天数</option></select></label>
          {draft.appeal_window_days != null && <label>公布后天数<input type="number" min={1} max={365} step={1} value={draft.appeal_window_days || ""} onChange={e => change("appeal_window_days", Number(e.target.value))} /></label>}</div>
        <label>申诉填写提示语<textarea aria-label="申诉填写提示语" maxLength={500} value={draft.appeal_prompt ?? "请说明你认为需要重新检查的地方"} onChange={e => change("appeal_prompt", e.target.value)} /></label>
        {actions("appeals")}
      </section>
      <RuntimeSettingsPanel />
    </div>
  </>;
}
