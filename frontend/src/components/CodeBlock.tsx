const TOKEN_PATTERN = /(\/\/[^\n]*|\/\*[\s\S]*?\*\/|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\b(?:alignas|alignof|and|asm|auto|bool|break|case|catch|char|class|const|constexpr|continue|default|delete|do|double|else|enum|explicit|extern|false|float|for|friend|goto|if|inline|int|long|namespace|new|nullptr|operator|or|private|protected|public|register|return|short|signed|sizeof|static|struct|switch|template|this|throw|true|try|typedef|typename|union|unsigned|using|virtual|void|volatile|while|def|elif|except|finally|from|import|in|is|lambda|None|not|pass|raise|range|True|False|with|yield)\b|\b\d+(?:\.\d+)?\b)/g;
const IS_TOKEN = /^(?:\/\/|\/\*|"|'|\d|alignas$|alignof$|and$|asm$|auto$|bool$|break$|case$|catch$|char$|class$|const$|constexpr$|continue$|default$|delete$|do$|double$|else$|enum$|explicit$|extern$|false$|float$|for$|friend$|goto$|if$|inline$|int$|long$|namespace$|new$|nullptr$|operator$|or$|private$|protected$|public$|register$|return$|short$|signed$|sizeof$|static$|struct$|switch$|template$|this$|throw$|true$|try$|typedef$|typename$|union$|unsigned$|using$|virtual$|void$|volatile$|while$|def$|elif$|except$|finally$|from$|import$|in$|is$|lambda$|None$|not$|pass$|raise$|range$|True$|False$|with$|yield$)/;

function tokenClass(token: string): string {
  if (token.startsWith("//") || token.startsWith("/*")) return "code-comment";
  if (token.startsWith('"') || token.startsWith("'")) return "code-string";
  if (/^\d/.test(token)) return "code-number";
  return "code-keyword";
}

export function CodeBlock({ code, language }: { code: string; language?: string }) {
  const normalized = code.replace(/\r\n?/g, "\n");
  const parts = normalized.split(TOKEN_PATTERN);
  return (
    <div className="source-wrap">
      {language && <div className="source-language">{language}</div>}
      <pre className="source-code"><span className="source-line-numbers" aria-hidden="true">{normalized.split("\n").map((_, index) => <span key={index} data-line={index + 1} />)}</span><code>{parts.map((part, index) =>
        IS_TOKEN.test(part)
          ? <span className={tokenClass(part)} key={index}>{part}</span>
          : <span key={index}>{part}</span>
      )}</code></pre>
    </div>
  );
}
