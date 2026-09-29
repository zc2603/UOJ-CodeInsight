const TOKEN_PATTERN = /(\/\/[^\n]*|\/\*[\s\S]*?\*\/|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\b(?:alignas|alignof|and|asm|auto|bool|break|case|catch|char|class|const|constexpr|continue|default|delete|do|double|else|enum|explicit|extern|false|float|for|friend|goto|if|inline|int|long|namespace|new|nullptr|operator|or|private|protected|public|register|return|short|signed|sizeof|static|struct|switch|template|this|throw|true|try|typedef|typename|union|unsigned|using|virtual|void|volatile|while|def|elif|except|finally|from|import|in|is|lambda|None|not|pass|raise|range|True|False|with|yield)\b|\b\d+(?:\.\d+)?\b)/g;
const IS_TOKEN = /^(?:\/\/|\/\*|"|'|\d|alignas$|alignof$|and$|asm$|auto$|bool$|break$|case$|catch$|char$|class$|const$|constexpr$|continue$|default$|delete$|do$|double$|else$|enum$|explicit$|extern$|false$|float$|for$|friend$|goto$|if$|inline$|int$|long$|namespace$|new$|nullptr$|operator$|or$|private$|protected$|public$|register$|return$|short$|signed$|sizeof$|static$|struct$|switch$|template$|this$|throw$|true$|try$|typedef$|typename$|union$|unsigned$|using$|virtual$|void$|volatile$|while$|def$|elif$|except$|finally$|from$|import$|in$|is$|lambda$|None$|not$|pass$|raise$|range$|True$|False$|with$|yield$)/;

function tokenClass(token: string): string {
  if (token.startsWith("//") || token.startsWith("/*")) return "code-comment";
  if (token.startsWith('"') || token.startsWith("'")) return "code-string";
  if (/^\d/.test(token)) return "code-number";
  return "code-keyword";
}

export function CodeBlock({ code, language, fontSize, wrap = false }: { code: string; language?: string; fontSize?: number; wrap?: boolean }) {
  const normalized = code.replace(/\r\n?/g, "\n");
  const parts = normalized.split(TOKEN_PATTERN);
  if (wrap) {
    const lines: { text: string; className: string }[][] = [[]];
    for (const part of parts) {
      part.split("\n").forEach((text, index) => {
        if (index) lines.push([]);
        lines[lines.length - 1].push({ text, className: IS_TOKEN.test(part) ? tokenClass(part) : "" });
      });
    }
    return <div className="source-wrap">{language && <div className="source-language">{language}</div>}
      <pre className="source-code source-code-wrapped" style={{ fontSize }}>{lines.map((line, index) =>
        <span className="wrapped-code-line" key={index}><span className="wrapped-line-number" aria-hidden="true" data-line={index + 1} />
          <code>{line.map((part, i) => <span className={part.className} key={i}>{part.text}</span>)}{index < lines.length - 1 ? "\n" : ""}</code></span>)}</pre></div>;
  }
  return (
    <div className="source-wrap">
      {language && <div className="source-language">{language}</div>}
      <pre className="source-code" style={{ fontSize }}><span className="source-line-numbers" aria-hidden="true">{normalized.split("\n").map((_, index) => <span key={index} data-line={index + 1} />)}</span><code>{parts.map((part, index) =>
        IS_TOKEN.test(part)
          ? <span className={tokenClass(part)} key={index}>{part}</span>
          : <span key={index}>{part}</span>
      )}</code></pre>
    </div>
  );
}
