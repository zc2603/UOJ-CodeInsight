export interface TeacherSettings {
  appeal_window_days: number | null;
  appeal_prompt: string;
  entry_minutes: number;
  reopen_minutes: number | null;
  time_mode: "per_question" | "fixed";
  minutes_per_question: number;
  fixed_minutes: number;
  question_template: "standard" | "all_choice" | "all_short";
  grade_bands: { label: string; minimum: number }[];
  result_filter: "all" | "finished" | "active" | "attention" | "pending";
  result_sort: "student" | "status" | "completed" | "score";
  result_columns: ("score" | "grade" | "confidence" | "completed" | "timeout")[];
  code_font_size: number;
  code_wrap: boolean;
  english_expanded: boolean;
}

export interface SettingsResponse { settings: TeacherSettings; revision: number; defaults?: TeacherSettings }

export const initialSettings: TeacherSettings = {
  appeal_window_days: null, appeal_prompt: "请说明你认为需要重新检查的地方",
  entry_minutes: 30, reopen_minutes: null, time_mode: "per_question", minutes_per_question: 4,
  fixed_minutes: 25, question_template: "standard",
  grade_bands: [{ label: "A+", minimum: 9 }, { label: "A", minimum: 7 }, { label: "B+", minimum: 5 },
    { label: "B", minimum: 3 }, { label: "C", minimum: 1 }, { label: "D", minimum: 0 }],
  result_filter: "all", result_sort: "student", result_columns: ["score", "grade", "confidence", "timeout"],
  code_font_size: 13, code_wrap: false, english_expanded: true,
};

export const columnLabels = { score: "原始分", grade: "等级", confidence: "最低置信度", completed: "完成时间", timeout: "超时标记" };
