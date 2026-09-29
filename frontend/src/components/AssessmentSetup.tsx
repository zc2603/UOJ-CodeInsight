import { ChevronDown, Clock3, SlidersHorizontal } from "lucide-react";

type Props = {
  problems: { problem_id: number; display_order?: number; title: string }[];
  choiceIds: number[];
  onChoices: (ids: number[]) => void;
  timeMode: "per_question" | "fixed";
  onTimeMode: (mode: "per_question" | "fixed") => void;
  perQuestionMinutes: number;
  onPerQuestionMinutes: (minutes: number) => void;
  fixedMinutes: number;
  onFixedMinutes: (minutes: number) => void;
};

export function AssessmentSetup(p: Props) {
  const count = p.problems.length + p.choiceIds.length;
  const minutes = p.timeMode === "fixed" ? p.fixedMinutes : count * p.perQuestionMinutes;
  return <div className="assessment-settings">
    <details className="assessment-disclosure">
      <summary><SlidersHorizontal size={19} /><span><strong>测评内容</strong><small>最多 {count} 问 · {p.problems.length} 道简答{p.choiceIds.length > 0 && ` + ${p.choiceIds.length} 道单选`}</small></span><span className="assessment-adjust">调整<ChevronDown size={16} /></span></summary>
      <div className="assessment-settings-body">
        <p className="assessment-hint">学生只回答有有效提交的原题，可按需调整每道原题的出题形式。</p>
        {p.problems.map((problem, index) => <div key={problem.problem_id} className="assessment-problem-row">
          <span className="assessment-order">{problem.display_order ?? index + 1}</span><label htmlFor={`question-format-${problem.problem_id}`}>{problem.title}</label>
          <select id={`question-format-${problem.problem_id}`} value={p.choiceIds.includes(problem.problem_id) ? "two" : "one"}
            onChange={event => p.onChoices(event.target.value === "two" ? [...p.choiceIds.filter(id => id !== problem.problem_id), problem.problem_id] : p.choiceIds.filter(id => id !== problem.problem_id))}>
            <option value="two">简答 + 单选</option><option value="one">仅简答</option>
          </select>
        </div>)}
      </div>
    </details>
    <details className="assessment-disclosure">
      <summary><Clock3 size={19} /><span><strong>个人作答时长</strong><small>{p.timeMode === "fixed" ? `每人 ${p.fixedMinutes || "—"} 分钟` : `每问 ${p.perQuestionMinutes || "—"} 分钟 · 最多 ${minutes || "—"} 分钟`}</small></span><span className="assessment-adjust">调整<ChevronDown size={16} /></span></summary>
      <div className="assessment-settings-body">
        <div className="assessment-time-modes" role="group" aria-label="时长计算方式">
          <button type="button" aria-pressed={p.timeMode === "per_question"} onClick={() => p.onTimeMode("per_question")}>按实际题量</button>
          <button type="button" aria-pressed={p.timeMode === "fixed"} onClick={() => p.onTimeMode("fixed")}>固定总时长</button>
        </div>
        <label className="assessment-minute-field"><span>{p.timeMode === "per_question" ? "每问时长" : "每人总时长"}</span><input type="number" min={1} max={180} step={1} required
          value={(p.timeMode === "per_question" ? p.perQuestionMinutes : p.fixedMinutes) || ""}
          onChange={event => (p.timeMode === "per_question" ? p.onPerQuestionMinutes : p.onFixedMinutes)(Number(event.target.value))} /><span>分钟</span></label>
        <p className="assessment-hint">{p.timeMode === "per_question" ? "按学生实际问题数计算总时长。" : "所有学生使用相同的总时长。"}时间可在各题之间自由分配，总时长不超过 180 分钟。</p>
        {(!Number.isInteger(minutes) || minutes < 1 || minutes > 180) && <p className="assessment-time-error" role="alert">请输入正整数分钟，并确保总时长不超过 180 分钟。</p>}
      </div>
    </details>
  </div>;
}
