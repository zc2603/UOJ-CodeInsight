import React from "react";
import ReactDOM from "react-dom/client";
import { ArrowRight, BookOpenCheck, ShieldCheck } from "lucide-react";
import { AdminApp } from "./pages/AdminApp";
import { StudentApp } from "./pages/StudentApp";
import "./styles.css";

function App() {
  const path = window.location.pathname;
  if (path.startsWith("/admin")) return <AdminApp />;
  const match = path.match(/^\/q\/([0-9a-f-]+)$/i);
  if (match) return <StudentApp quizId={match[1]} />;
  return (
    <main className="center-page public-home">
      <div className="auth-brand">
        <span className="brand-mark"><BookOpenCheck size={24} /></span>
        <span><strong>UOJ 课程辅助系统</strong><small>代码理解测评平台</small></span>
      </div>
      <section className="card home-card">
        <div className="role-label"><BookOpenCheck size={16} /> 程序设计课程测评</div>
        <h1>从“代码通过”到“真正理解”</h1>
        <p>本平台根据学生在 UOJ Contest 中的有效提交，进行简短、逐题的代码理解测评。</p>
        <div className="home-student-note"><strong>学生如何进入？</strong><span>请通过教师提供的专属链接参与测评。</span></div>
        <a className="button secondary home-admin-link" href="/admin"><ShieldCheck size={16} />进入教师管理端<ArrowRight size={15} /></a>
      </section>
      <div className="auth-footer">UOJ 课程辅助服务</div>
    </main>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode><App /></React.StrictMode>,
);
