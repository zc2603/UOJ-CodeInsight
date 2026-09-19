export type AttemptStatus =
  | "PREPARING"
  | "IN_PROGRESS"
  | "GRADING"
  | "FINISHED"
  | "EXPIRED"
  | "RESET"
  | "GRADING_ERROR";

export interface StudentQuestion {
  timed_out: boolean;
  draft_answer: string;
  draft_revision: number;
  server_time: string | null;
  attempt_id: string;
  status: AttemptStatus;
  problem_id: number;
  problem_title: string;
  problem_statement: string;
  source_code: string;
  language: string;
  deadline_at: string | null;
  question_index: number | null;
  question_count: number;
  problem_question_index: number | null;
  question_type: string | null;
  question_text: string | null;
  question_text_en: string | null;
}

export interface PreparationProgress {
  total: number; completed: number; running: number; queued: number; failed: number; cancelled: number;
  students_total: number; students_ready: number; ready: boolean;
}

export interface QuizSummary {
  pre_generated: boolean;
  preparation: PreparationProgress | null;
  id: string;
  name: string;
  uoj_contest_id: number;
  start_time: string;
  end_time: string;
  duration_minutes: number;
  status: string;
  participant_count: number;
  finished_count: number;
  average_score: number | null;
}

export interface ResultRow {
  timed_out: boolean;
  review_required: boolean;
  prepared_problem_count: number;
  preparation_total: number;
  student_number: string;
  participant_status: string;
  attempt_id: string | null;
  attempt_status: string | null;
  problem_count: number;
  question_count: number;
  auto_score: number | null;
  manual_score: number | null;
  final_score: number | null;
  max_score: number;
  final_percent: number | null;
  confidence: number | null;
}

export interface AttemptQuestionDetail {
  review_required?: boolean;
  review_reason?: string | null;
  question_validity?: string | null;
  index: number;
  type: string;
  question: string;
  question_en: string | null;
  problem: { id: number; title: string; statement: string };
  source_code: string;
  language: string;
  reference_answer: string;
  grading_points: string[];
  student_answer: string | null;
  score: number | null;
  reason: string | null;
  confidence: number | null;
}

export interface AttemptDetail {
  review_required: boolean;
  id: string;
  status: string;
  student_number: string;
  auto_score: number | null;
  max_score: number;
  final_percent: number | null;
  manual_override_score: number | null;
  manual_override_reason: string | null;
  questions: AttemptQuestionDetail[];
}

export interface PreparedDetail {
  student_number: string;
  round_no: number;
  prepared_problem_count: number;
  preparation_total: number;
  questions: AttemptQuestionDetail[];
}
