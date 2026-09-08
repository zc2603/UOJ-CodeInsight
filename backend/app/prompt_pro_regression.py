"""One authorized, synthetic cross-topic Pro evaluation. No production DB writes."""
from __future__ import annotations
import argparse
import asyncio
import json
import time
from pathlib import Path

from app.config import get_settings
from app.services.llm_provider import create_llm_provider, LLMProviderError, GENERATOR_VERSION, GRADER_VERSION
from app.prompt_review_regression import STATEMENT, SOURCE, QUESTION

SESSION = "20260908-pro-v10"
CASES = [
    dict(name="array",kind="trace",title="数组插入",statement=STATEMENT,source=SOURCE),
    dict(name="stack",kind="boundary",title="括号匹配",statement="输入一个长度为1到8、仅由左右圆括号组成的字符串。判断是否完全匹配，输出YES或NO。",source='''#include <iostream>
#include <stack>
#include <string>
using namespace std;
int main(){string t;cin>>t;stack<char>s;bool ok=true;
for(char c:t){if(c=='(')s.push(c);else{if(s.empty()){ok=false;break;}s.pop();}}
cout<<(ok&&s.empty()?"YES":"NO")<<"\\n";}'''),
    dict(name="queue",kind="modification",title="有向图最短路",statement="首行n,m，1<=n<=4，0<=m<=n*n。接下来m行有向边u,v，1<=u,v<=n，允许重边和自环。输出从1到n的最少边数，不可达输出-1。",source='''#include <iostream>
#include <vector>
#include <queue>
using namespace std;
int main(){int n,m;cin>>n>>m;vector<int>g[5];while(m--){int u,v;cin>>u>>v;g[u].push_back(v);}
int d[5];for(int i=1;i<=n;i++)d[i]=-1;queue<int>q;d[1]=0;q.push(1);
while(!q.empty()){int u=q.front();q.pop();for(int v:g[u])if(d[v]==-1){d[v]=d[u]+1;q.push(v);}}
cout<<d[n]<<"\\n";}'''),
]


def fixed_checks():
    array = [
        dict(question_index=1,question=QUESTION,student_answer="1 5 2 3\n3"),
        dict(question_index=2,question=QUESTION,student_answer="1 5 2 3\n2"),
        dict(question_index=3,question="给定输入：\n1 1\n0\n5\n程序输出什么？",student_answer="数组元素0不满足题面要求的1到100，输入不合法。"),
    ]
    return [
        ("array_repeat_1",CASES[0],array,[(1,False),(2,False),(2,True)]),
        ("array_repeat_2",CASES[0],array,[(1,False),(2,False),(2,True)]),
        ("stack_reason",CASES[1],[dict(question_index=1,question="处理右括号时，为什么在s.pop()之前检查s.empty()？",student_answer="空栈说明当前右括号没有对应的左括号，应判定不匹配；也避免对空栈执行弹出操作。")],[(2,False)]),
        ("queue_reason_and_output",CASES[2],[
            dict(question_index=1,question="为什么在q.push(v)之前就给d[v]赋值？",student_answer="记录首次发现的距离并标记已发现，后续边就不会重复将它入队。广度优先搜索首次发现的距离就是最短距离。"),
            dict(question_index=2,question="给定输入：\n3 2\n1 2\n2 3\n程序输出什么？",student_answer="2"),
        ],[(2,False),(2,False)]),
    ]


async def run(output):
    output.mkdir(parents=True,exist_ok=False)
    settings=get_settings().model_copy(update={"llm_max_concurrency":1})
    if settings.llm_provider=="mock" or settings.llm_model!="deepseek-v4-pro":
        raise ValueError("The authorized model must be deepseek-v4-pro")
    prior=[]
    for path in output.parent.glob("*/report.json"):
        old=json.loads(path.read_text(encoding="utf-8"))
        if old.get("session")==SESSION:prior.append(old)
    prior_count=sum(r["http_requests"] for r in prior)
    prior_reserve=sum(r["reserved_peak_cny"] for r in prior)
    report=dict(session=SESSION,model=settings.llm_model,generator=GENERATOR_VERSION,grader=GRADER_VERSION,
        http_requests=0,reserved_peak_cny=0.0,requests=[],generation=[],grading=[],prior_requests=prior_count,
        max_requests=10,max_cost_cny=3,reasoning_effort=settings.llm_reasoning_effort)
    def save():
        (output/"report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    provider=create_llm_provider(settings)
    async def before(request):
        reserve=((len(request.content)+512)*9+settings.llm_max_tokens*27)/1_000_000
        if prior_count+report["http_requests"]>=10 or prior_reserve+report["reserved_peak_cny"]+reserve>3:
            raise LLMProviderError("Approved Pro regression budget exhausted")
        report["http_requests"]+=1;report["reserved_peak_cny"]+=reserve
        report["requests"].append(dict(payload=json.loads(request.content),reserved_peak_cny=reserve))
        save()
    async def after(response):
        await response.aread()
        body=response.json()
        record=report["requests"][-1]
        record.update(status=response.status_code,usage=body.get("usage",{}),
            content=body.get("choices",[{}])[0].get("message",{}).get("content"))
        save()
    provider.client.event_hooks={"request":[before],"response":[after]}
    try:
        for case in CASES:
            (output/(case["name"]+".cpp")).write_text(case["source"],encoding="utf-8")
            begin=time.monotonic()
            generated,raw=await provider.generate_questions(title=case["title"],statement=case["statement"],language="C++",
                source_code=case["source"],second_question_kind=case["kind"])
            report["generation"].append(dict(name=case["name"],kind=case["kind"],seconds=time.monotonic()-begin,
                questions=generated.model_dump(mode="json"),raw=raw))
            save();print(f"generation={case['name']} kind={case['kind']} ok",flush=True)
        for name,case,payload,expected in fixed_checks():
            begin=time.monotonic()
            result,raw=await provider.grade_answers(title=case["title"],statement=case["statement"],language="C++",
                source_code=case["source"],question_payload=payload)
            passed=len(result.grades)==len(expected) and all((g.score,g.review_required)==e for g,e in zip(result.grades,expected))
            report["grading"].append(dict(name=name,seconds=time.monotonic()-begin,result=result.model_dump(mode="json"),raw=raw,passed=passed))
            save();print(f"grading={name} passed={passed}",flush=True)
        report["grading_passed"]=all(item["passed"] for item in report["grading"])
    except Exception as exc:
        report["error_type"]=type(exc).__name__
        raise
    finally:
        report["estimated_peak_cny"]=sum((r.get("usage",{}).get("prompt_cache_hit_tokens",0)*.3+
            (r.get("usage",{}).get("prompt_tokens",0)-r.get("usage",{}).get("prompt_cache_hit_tokens",0))*9+
            r.get("usage",{}).get("completion_tokens",0)*27)/1_000_000 for r in report["requests"])
        save();await provider.close()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--confirm-real-cost",action="store_true")
    args=parser.parse_args()
    if not args.confirm_real_cost:parser.error("Explicit cost approval required")
    try:asyncio.run(run(args.output))
    except Exception as exc:
        print(f"regression=failed reason={type(exc).__name__}",flush=True)
        raise SystemExit(1)


if __name__=="__main__":main()
