/** Make accidental double-escaped whitespace readable without changing quoted code like '\\n'. */
export function readableGeneratedQuestion(value: string | null | undefined): string {
  if (!value) return "";
  return value
    .replace(/(^|[^"'`])\\r\\n/g, "$1\n")
    .replace(/(^|[^"'`])\\n/g, "$1\n")
    .replace(/(^|[^"'`])\\t/g, "$1    ");
}
