import { memo } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeRaw from "rehype-raw";
import rehypeSanitize, { defaultSchema } from "rehype-sanitize";
import rehypeKatex from "rehype-katex";
import "katex/dist/katex.min.css";

const schema = {
  ...defaultSchema,
  attributes: {
    ...defaultSchema.attributes,
    code: [...(defaultSchema.attributes?.code ?? []), ["className", /^language-./, "math-inline", "math-display"]],
  },
};

export const ProblemStatement = memo(function ProblemStatement({ text, className = "" }: { text: string; className?: string }) {
  return <div className={`markdown-statement ${className}`}>
    <Markdown remarkPlugins={[remarkGfm, remarkMath]}
      rehypePlugins={[rehypeRaw, [rehypeSanitize, schema], [rehypeKatex, { trust: false, strict: "ignore", maxExpand: 1000 }]]}>
      {text}
    </Markdown>
  </div>;
});
