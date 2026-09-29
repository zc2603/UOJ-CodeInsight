// Synthetic UI checks. All API requests are intercepted; no database/model access.
// Run after building: PLAYWRIGHT_MODULE=<installed playwright path> node tests/interface.browser.cjs
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const dist = path.resolve(__dirname, '../dist');
const output = path.resolve(process.env.UI_EVIDENCE_DIR || path.join(__dirname, '../../artifacts/ui-refresh-20260929'));
const quizId = '00000000-0000-4000-8000-000000000001';
const source = Array.from({length: 100}, (_, i) => `// synthetic source line ${i + 1}`).join('\n');
const sizes = [{width:1366,height:768},{width:900,height:768},{width:390,height:844}];
const emptyDraft = () => ({answer_text:'',choice_id:null,revisit:false,dispute:false,dispute_reason:null,revision:0});
const questions = () => [1,2,3].map(n => ({id:`question-${n}`,index:n,type:n===2?'trace':'explanation',
  response_format:n===2?'single_choice':'short_answer',question:n===2?'执行 `sum += 2` 后，`sum` 的值是什么？':'为什么更新后，`sum` 表示当前已经读入的数的总和？',
  question_en:n===2?'What is sum after `sum += 2`?':'Why does sum represent the sum of the values read so far?',
  choices:n===2?['A','B','C','D'].map((id,i)=>({id,text:`${i+1}`,text_en:`${i+1}`})):null,
  problem_id:n===3?103:101,problem_title:'合成课堂练习 · 累加与状态更新',
  problem_statement:'## 题目描述\n\n给定 $n$ 个整数，计算它们的总和。\n\n### 输入格式\n\n第一行包含整数 $n$，第二行包含 $n$ 个整数。\n\n### 输出格式\n\n输出总和。\n\n```text\n3\n1 2 3\n```\n\n**样例输出**\n\n```text\n6\n```',
  source_code:source,language:'C++',draft:emptyDraft()}));
function fixture() {
  const qs=questions();
  return {questions:qs,resultMode:'waiting',loggedIn:true,submissions:[],appeals:[],calls:[],
    quizzes:[{id:quizId,name:'合成课堂测评',uoj_contest_id:7,pre_generated:true,preparation:null,
      assessment_version:'lightweight_v1',scores_published:false,status:'CLOSED',participant_count:3,finished_count:2,
      average_score:83.3,start_time:'2026-09-29T01:00:00Z',end_time:'2026-09-29T02:00:00Z'},
      {id:'draft',name:'课前准备 · 合成测评',uoj_contest_id:8,pre_generated:true,
        preparation:{total:3,completed:3,completed_questions:5,running:0,queued:0,failed:0,cancelled:0,students_total:1,students_ready:1,ready:true},
        assessment_version:'lightweight_v1',scores_published:false,status:'DRAFT',participant_count:1,finished_count:0,average_score:null,
        start_time:'2026-09-29T01:00:00Z',end_time:'2026-09-29T02:00:00Z'}]};
}
function resultData(f) {
  if(f.resultMode==='waiting') return {published:false};
  if(f.resultMode==='absent') return {published:true,participated:false};
  return {published:true,participated:true,score:5,max_score:6,percent:83.333,submitted_at:'2026-09-29T01:35:00Z',submission_source:'manual',
    questions:f.questions.map((q,i)=>({...q,answer_text:i===1?'':'每次把当前读到的数加进去，所以它保存了前面所有数的和。',
      choice_id:i===1?'B':null,score:i===2?1:2,reason:'回答已说明累加更新的作用，体现了对局部状态的理解。',
      reference_answer:'每次读入一个数 $a_i$，执行 `sum += a_i`，使 `sum` 保持为已读入数值的总和。',
      correct_choice_id:i===1?'B':null,score_version:1,appeals:f.appeals.filter(a=>a.question_id===q.id)}))};
}
function teacherQuestions(f) { return f.questions.map(q=>({...q,problem:{id:q.problem_id,title:q.problem_title,statement:q.problem_statement},
  reference_answer:'将新读入的值加入之前的总和。',core_idea:q.choices?null:'理解累加状态',grading_points:[],correct_choice_id:q.choices?'B':null,
  student_answer:'将新读入的数加进去。',choice_id:q.choices?'B':null,score:2,effective_score:2,reason:'体现了核心理解。',confidence:.95,
  review_required:false,question_en:q.question_en,job_id:'job',local_index:q.index,revision:'revision-1'})); }
async function wire(page,f,errors) {
  page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/*',async route=>{
    const req=route.request(),u=new URL(req.url()),p=u.pathname;
    assert.equal(u.hostname,'quiz.test');
    if(!p.startsWith('/api/')) {
      const asset=p.startsWith('/assets/')?p:'/index.html';
      let body=fs.readFileSync(path.join(dist,asset));
      if(process.env.UI_ASSETS_ORIGIN) {
        const response=await fetch(new URL(asset,process.env.UI_ASSETS_ORIGIN));
        assert.equal(response.status,200);
        const remote=Buffer.from(await response.arrayBuffer());
        assert.ok(body.equals(remote),`Deployed asset mismatch: ${asset}`);body=remote;
      }
      return route.fulfill({body,contentType:asset.endsWith('.js')?'text/javascript':asset.endsWith('.css')?'text/css':asset.endsWith('.html')?'text/html':'application/octet-stream'});
    }
    f.calls.push(`${req.method()} ${p}`);
    let data;
    if(p.endsWith('/login-options')) data={uoj_password_enabled:false};
    else if(p.match(/^\/api\/quiz\/.*\/login$/)) data={quiz_name:'合成课堂测评',pre_generated:true,result_only:false,assessment_version:f.legacy?'legacy':'lightweight_v1',question_count:3,duration_minutes:15};
    else if(p.endsWith('/start')||p==='/api/attempt/current') data={assessment_version:'lightweight_v1',attempt_id:'synthetic-attempt',status:'IN_PROGRESS',question_index:1,question_count:3,
      questions:f.questions,deadline_at:new Date(Date.now()+900000).toISOString(),server_time:new Date().toISOString(),
      ...(f.legacy?{assessment_version:'legacy',problem_id:101,problem_title:f.questions[0].problem_title,problem_statement:f.questions[0].problem_statement,
        source_code:source,language:'C++',problem_question_index:1,question_type:'explanation',question_text:f.questions[0].question,question_text_en:f.questions[0].question_en}: {})};
    else if(p==='/api/attempt/current/draft') { assert.ok(f.legacy,'New protocol used the legacy draft route');data={saved:true}; }
    else if(p==='/api/attempt/synthetic-attempt/draft') {
      const b=req.postDataJSON(),q=f.questions.find(q=>q.id===b.question_id);
      assert.ok(q);assert.equal(b.expected_revision,q.draft.revision);
      q.draft={...b,revision:q.draft.revision+1};data={saved:true,draft:q.draft};
    } else if(p.endsWith('/submit')) {
      f.submissions.push(req.postDataJSON());
      if(f.submissions.length===1) return route.fulfill({status:503,json:{detail:'合成网络重试'}});
      data={source:'manual'};
    } else if(p.endsWith('/my-result')) {
      if(f.resultMode==='error') return route.fulfill({status:503,json:{detail:'合成网络错误，请稍后重试'}});
      data=resultData(f);
    } else if(p.endsWith('/appeal')) {
      const b=req.postDataJSON();assert.ok(b.reason.trim());assert.match(b.idempotency_key,/^[0-9a-f-]{36}$/);
      f.appeals.push({id:'appeal-1',question_id:p.split('/').at(-2),state:'pending',reason:b.reason,resolution:null,question_score_version:1});data={id:'appeal-1'};
    } else if(p==='/api/admin/login') { f.loggedIn=true;data={}; }
    else if(p==='/api/admin/quizzes') {
      if(!f.loggedIn)return route.fulfill({status:401,json:{detail:'登录后继续'}});
      data=f.quizzes;
    } else if(p==='/api/admin/overview') data={student_count:3};
    else if(p==='/api/admin/features') data={lightweight_creation_enabled:true,quality_audit_enabled:false};
    else if(p.endsWith('/preview-contest')) data={contest_name:'合成课堂测评',submission_cutoff:'2026-09-28T01:00:00Z',cutoff_reached:true,
      problems:[101,102,103].map((id,i)=>({problem_id:id,display_order:i+1,title:`第 ${i+1} 题 · 合成状态更新`,include_choice:i<2})),
      numeric_student_accounts:3,students_with_eligible_problem:3,selected_submission_snapshots:8,parser_errors:[],roster:null};
    else if(p.endsWith('/results')) data=[1,2,3].map((i)=>({student_number:`20990000${i}`,participant_status:'READY',attempt_id:i<3?'attempt-'+i:null,
      attempt_status:i<3?'FINISHED':null,problem_count:2,question_count:3,auto_score:i<3?6:null,manual_score:null,final_score:i<3?6:null,
      max_score:6,final_percent:i<3?100:null,confidence:.95,review_required:false,timed_out:false,prepared_problem_count:2,preparation_total:2}));
    else if(p.endsWith('/appeals')) data=[{id:'teacher-appeal',attempt_id:'attempt-1',question_id:'question-1',state:'pending',
      reason:'我用自己的话说明了更新作用，希望老师再核对一下。',score_snapshot:1,question_snapshot:f.questions[0].question,
      answer_snapshot:'每次都把当前读入的数加上。',resolution:null,created_at:'2026-09-29T02:00:00Z'}];
    else if(p.endsWith('/prepared-questions')) data={can_edit:true,student_number:'209900003',round_no:1,prepared_problem_count:2,preparation_total:2,questions:teacherQuestions(f)};
    else if(p.startsWith('/api/admin/attempts/')) data={id:'attempt-1',assessment_version:'lightweight_v1',student_number:'209900001',status:'FINISHED',completed_at:'2026-09-29T01:35:00Z',
      timed_out:false,review_required:false,auto_score:6,max_score:6,final_percent:100,manual_override_score:null,manual_override_reason:null,score_version:1,questions:teacherQuestions(f)};
    else if(p.endsWith('/logout')) data={};
    else throw Error(`Unmocked request: ${req.method()} ${p}`);
    await route.fulfill({json:data});
  });
}
async function capture(page,name,size) {
  await page.evaluate(()=>document.fonts.ready);
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),`Horizontal overflow: ${name} ${size.width}`);
  await page.screenshot({path:path.join(output,`${name}-${size.width}.png`),fullPage:true,animations:'disabled'});
}
async function focusTrap(page) {
  const dialog=page.getByRole('dialog');
  await dialog.waitFor();
  for(let i=0;i<7;i++) { await page.keyboard.press('Tab');assert.ok(await dialog.evaluate(el=>el.contains(document.activeElement))); }
  await page.keyboard.press('Shift+Tab');assert.ok(await dialog.evaluate(el=>el.contains(document.activeElement)));
}
(async()=>{
  fs.mkdirSync(output,{recursive:true});
  const browser=await chromium.launch({channel:'chrome',headless:true});
  const errors=[];
  try {
    for(const size of sizes) {
      const context=await browser.newContext({viewport:size}),page=await context.newPage(),f=fixture();
      await wire(page,f,errors);
      await page.goto(`http://quiz.test/q/${quizId}`);
      await page.getByLabel('UOJ 用户名').waitFor();
      assert.deepEqual(await page.evaluate(()=>[isSecureContext,typeof crypto.randomUUID]),[false,'undefined']);
      await capture(page,'student-login',size);
      await page.getByLabel('UOJ 用户名').fill('synthetic-student');await page.getByLabel('备用测评码').fill('TESTCODE');
      await page.getByRole('button',{name:'验证身份并继续'}).click();await page.getByRole('heading',{name:'开始前请确认'}).waitFor();
      await capture(page,'student-ready',size);
      await page.getByRole('button',{name:'确认并开始测评'}).click();
      await page.getByLabel('你的回答').fill('每次把读入的数加入之前的总和。');
      await page.waitForTimeout(650);
      await page.getByLabel('稍后再看',{exact:true}).check();await page.waitForTimeout(550);
      await page.getByLabel('稍后再看',{exact:true}).uncheck();
      assert.ok(!await page.getByRole('button',{name:/问题 1，/}).getAttribute('aria-label').then(s=>s.includes('稍后再看')));
      if(size.width>760) {
        const nav=await page.getByRole('navigation',{name:'问题导航'}).boundingBox(),answer=await page.getByRole('region',{name:'作答区域'}).boundingBox();
        assert.ok(nav.y+nav.height<=answer.y+1,'Navigation overlaps the answer panel');
        await page.locator('.source-code').evaluate(el=>{el.scrollTop=el.scrollHeight;});
        assert.ok(await page.locator('.source-code').evaluate(el=>Math.abs(el.scrollHeight-el.clientHeight-el.scrollTop)<2),'Code bottom is clipped');
      }
      await capture(page,'student-answer',size);
      await page.getByRole('button',{name:'下一题',exact:true}).click();
      await page.getByRole('radio').first().focus();await page.keyboard.press('ArrowRight');
      await page.getByRole('radio').nth(1).waitFor();assert.ok(await page.getByRole('radio').nth(1).isChecked());
      await capture(page,'student-choice',size);
      await page.getByRole('button',{name:'清空选择'}).click();assert.equal(await page.getByRole('radio').evaluateAll(els=>els.filter(el=>el.checked).length),0);
      await page.getByRole('radio').nth(1).check();
      await page.getByRole('button',{name:/问题 1，/}).focus();await page.keyboard.press('Enter');
      assert.equal(await page.getByLabel('你的回答').inputValue(),'每次把读入的数加入之前的总和。');
      await page.getByRole('button',{name:/问题 3，/}).click();await page.getByRole('button',{name:'检查并交卷'}).click();
      await focusTrap(page);await capture(page,'student-submit-dialog',size);await page.keyboard.press('Escape');
      assert.equal(await page.getByRole('dialog').count(),0);
      await page.getByRole('button',{name:'检查并交卷'}).click();await page.getByRole('button',{name:'确认交卷',exact:true}).click();
      await page.getByRole('alert').filter({hasText:'合成网络重试'}).waitFor();
      await page.getByRole('button',{name:'检查并交卷'}).click();await page.getByRole('button',{name:'确认交卷',exact:true}).click();
      await page.getByRole('heading',{name:'全部答案已提交'}).waitFor();await capture(page,'student-done',size);
      assert.equal(f.submissions[0].idempotency_key,f.submissions[1].idempotency_key);
      assert.equal(f.submissions[1].drafts[1].choice_id,'B');
      assert.ok(!f.calls.some(c=>c.includes('/current/draft')),'Lightweight attempted a legacy draft save');
      await page.getByRole('button',{name:'查看成绩状态'}).click();await page.getByRole('heading',{name:'成绩尚未公布'}).waitFor();
      await capture(page,'student-results-waiting',size);
      f.resultMode='published';await page.getByRole('button',{name:'刷新状态'}).click();await page.getByRole('heading',{name:'逐题回顾'}).waitFor();
      await capture(page,'student-results-published',size);
      await page.getByRole('button',{name:'申请复核',exact:true}).first().click();
      await page.getByLabel('请说明你认为需要重新检查的地方').fill('希望教师核对我的口语表述。');
      await capture(page,'student-appeal',size);await page.getByRole('button',{name:'提交申请'}).click();
      await page.getByText('复核申请 · 处理中',{exact:true}).waitFor();assert.ok(await page.getByRole('button',{name:'申请复核',exact:true}).first().isDisabled());
      f.resultMode='absent';await page.getByRole('button',{name:'刷新状态'}).click();await page.getByRole('heading',{name:'本次测评未参加'}).waitFor();await capture(page,'student-absent',size);
      f.resultMode='error';await page.getByRole('button',{name:'刷新状态'}).click();await page.getByRole('alert').filter({hasText:'合成网络错误'}).waitFor();await capture(page,'student-result-error',size);

      f.loggedIn=false;await page.goto('http://quiz.test/admin');await page.getByRole('heading',{name:'欢迎回来'}).waitFor();await capture(page,'teacher-login',size);
      await page.getByLabel('用户名',{exact:true}).fill('synthetic-teacher');await page.getByLabel('密码',{exact:true}).fill('synthetic-password');
      await page.getByRole('button',{name:'登录管理端'}).click();await page.getByRole('heading',{name:'全部测评'}).waitFor();await capture(page,'teacher-list',size);
      await page.getByRole('button',{name:'创建测评',exact:true}).first().click();await page.getByRole('heading',{name:'导入比赛'}).waitFor();
      await page.getByLabel('Contest ID').fill('7');await page.getByRole('button',{name:'导入并预览'}).click();await page.getByText('最多 5 问 · 3 道简答 + 2 道单选').waitFor();
      await capture(page,'teacher-create',size);await page.locator('.assessment-disclosure').nth(1).locator('summary').click();
      await page.getByRole('button',{name:'固定总时长'}).click();await page.locator('.assessment-minute-field input').fill('30');
      await capture(page,'teacher-settings',size);
      await page.getByRole('button',{name:'测评管理',exact:true}).click();await page.locator('.quiz-table').getByRole('button',{name:'查看结果'}).first().click();
      await page.getByRole('heading',{name:'学生成绩',exact:true}).waitFor();await capture(page,'teacher-results',size);
      await page.getByLabel('搜索学号').fill('209900001');assert.equal(await page.locator('.result-table tbody tr').count(),1);await page.getByLabel('搜索学号').fill('');
      await page.getByRole('button',{name:'处理申诉'}).click();await focusTrap(page);await capture(page,'teacher-appeal-dialog',size);await page.keyboard.press('Escape');
      await page.getByRole('button',{name:'完整作答'}).click();await page.getByRole('heading',{name:'逐题成绩',exact:true}).waitFor();
      await page.locator('.material-details summary').first().click();await capture(page,'teacher-attempt',size);
      await page.getByRole('button',{name:'返回测评结果'}).click();
      await page.locator('.result-table tbody tr').filter({hasText:'209900003'}).getByRole('button',{name:'查看详情'}).click();
      await page.getByText('已准备 2 / 2 道题目，共 3 个问题').waitFor();await capture(page,'teacher-prepared',size);
      await page.getByRole('button',{name:'编辑问题'}).first().click();await page.getByRole('dialog').waitFor();await capture(page,'teacher-edit-dialog',size);
      f.legacy=true;await page.goto(`http://quiz.test/q/${quizId}`);
      await page.getByLabel('UOJ 用户名').fill('synthetic-student');await page.getByLabel('备用测评码').fill('TESTCODE');
      await page.getByRole('button',{name:'验证身份并继续'}).click();await page.getByRole('button',{name:'确认并开始测评'}).click();
      await page.getByLabel('你的回答').fill('合成旧版答案');await capture(page,'student-legacy',size);
      await context.close();console.log(`PASS: student/teacher workflows and screenshots at ${size.width}×${size.height}`);
    }
    assert.deepEqual(errors,[]);
    console.log('PASS: no browser exceptions; HTTP compatibility, persistence, submission, appeals, keyboard, navigation and overflow checks');
  } finally { await browser.close(); }
})().catch(e=>{console.error(e);process.exitCode=1;});
