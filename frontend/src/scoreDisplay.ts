/** Shared student/teacher grade bands, based on the raw score, not a percentage. */
export function scoreGrade(score: number | null | undefined): string {
  if (score == null || !Number.isFinite(score) || score < 0) return "—";
  if (score >= 9) return "A+";
  if (score >= 7) return "A";
  if (score >= 5) return "B+";
  if (score >= 3) return "B";
  if (score >= 1) return "C";
  return "D";
}
