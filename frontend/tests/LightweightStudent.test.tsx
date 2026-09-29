import { renderToStaticMarkup } from "react-dom/server";
import { LightweightStudent } from "../src/pages/LightweightStudent";
import { createRequestId } from "../src/requestId";
import type { StudentQuestion } from "../src/types";

// Plain HTTP browsers expose getRandomValues, but not randomUUID.
const original = Object.getOwnPropertyDescriptor(globalThis, "crypto");
const cryptoApi = globalThis.crypto;
Object.defineProperty(globalThis, "crypto", { configurable: true, value: {
  getRandomValues: cryptoApi.getRandomValues.bind(cryptoApi),
} });
try {
  const ids = Array.from({ length: 100 }, createRequestId);
  if (new Set(ids).size !== ids.length || ids.some(id => !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(id))) {
    throw new Error("HTTP fallback must produce distinct UUID v4 request IDs");
  }
  const initial = {
    assessment_version: "lightweight_v1", attempt_id: "synthetic-attempt",
    deadline_at: new Date(Date.now() + 900000).toISOString(),
    server_time: new Date().toISOString(),
    questions: [{
      id: "synthetic-question", index: 1, type: "explanation", response_format: "short_answer",
      question: "解释变量的作用", question_en: "Explain the variable", choices: null,
      problem_id: 1, problem_title: "合成题目", problem_statement: "输入一个整数。",
      source_code: "int main() { return 0; }", language: "C++",
      draft: { answer_text: "", choice_id: null, revisit: false, dispute: false,
        dispute_reason: null, revision: 0 },
    }],
  } as StudentQuestion;
  const html = renderToStaticMarkup(<LightweightStudent quizId="synthetic-quiz" initial={initial} onDone={() => {}} />);
  if (!html.includes("解释变量的作用") || !html.includes("你的回答")) throw new Error("Answer page did not render");
  console.log("LightweightStudent: renders with the plain HTTP Web Crypto API");
} finally {
  if (original) Object.defineProperty(globalThis, "crypto", original);
}
if (!createRequestId()) throw new Error("Secure-context request ID failed");
