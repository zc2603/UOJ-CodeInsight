import { renderToStaticMarkup } from "react-dom/server";
import { ProblemStatement } from "../src/components/ProblemStatement";

const render = (text: string) => renderToStaticMarkup(<ProblemStatement text={text} />);
function check(condition: boolean, message: string) { if (!condition) throw new Error(message); }
const rich = render("## 输入格式\n\n**整数** $n^2$\n\n$$\n\\sum_{i=1}^n i\n$$\n\n- 一行\n- 两列\n\n| 输入 | 输出 |\n| --- | --- |\n| 1 | 2 |\n\n```cpp\nint x = 1;\n``` ");
check(rich.includes("<h2>") && rich.includes("<strong>") && rich.includes("<table>") && rich.includes("<ul>"), "Markdown structure");
check(rich.includes('class="katex"') && rich.includes("katex-display") && rich.includes("<math"), "Inline and display formulas");
check(render("`$n$`\n\n```\n$x$\n```").includes("$n$") && !render("`$n$`").includes('class="katex"'), "Code must remain literal");
const hostile = render('<script>alert(1)</script><img src="x" onerror="alert(1)"><a href="javascript:alert(1)">bad</a><iframe src="x"></iframe>');
check(!hostile.includes("<script") && !hostile.includes("onerror") && !hostile.includes("javascript:") && !hostile.includes("<iframe"), "Unsafe HTML stripped");
check(render("<h3>旧题面</h3><pre>1 2\n3 4</pre>").includes("<h3>旧题面</h3>"), "Legacy HTML supported");
check(render("$\\invalidcommand{x}$").length > 0, "Invalid formula must not crash");
console.log("ProblemStatement: 6 rendering and safety checks passed");
