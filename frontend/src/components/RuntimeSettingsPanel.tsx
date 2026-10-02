import { useEffect, useState } from "react";
import { api } from "../api";

type Service = "deepseek" | "openai";
interface Options {
  generation_service: Service; grading_service: Service;
  deepseek_model: string; openai_model: string;
  reasoning_effort: "low" | "high" | "max";
  max_tokens: number; request_timeout_seconds: number; generation_timeout_seconds: number;
  generation_max_attempts: number; generation_global_concurrency: number; grading_timeout_seconds: number;
}
interface Snapshot {
  settings: Options; defaults: Options; revision: number;
  services: Record<Service, { base_url: string; configured: boolean; models: string[] }>;
  runtime: { active_workers: number; loaded_workers: number; poll_seconds: number; mode: string;
    request_concurrency_per_process: number; generation_workers_per_process: number; grading_workers_per_process: number };
}

export function RuntimeSettingsPanel() {
  const [data, setData] = useState<Snapshot | null>(null);
  const [draft, setDraft] = useState<Options | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [status, setStatus] = useState<Snapshot | null>(null);
  async function reload() {
    setBusy(true); setError("");
    try {
      const fresh = await api<Snapshot>("/api/admin/runtime-settings");
      setData(fresh); setDraft(fresh.settings); setStatus(fresh); setNotice("");
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  useEffect(() => { void reload(); }, []);
  useEffect(() => {
    let active = true;
    const timer = window.setInterval(() => {
      void api<Snapshot>("/api/admin/runtime-settings").then(fresh => { if (active) setStatus(fresh); }).catch(() => { if (active) setStatus(null); });
    }, 5000);
    return () => { active = false; window.clearInterval(timer); };
  }, []);
  function change<K extends keyof Options>(key: K, value: Options[K]) {
    setDraft(old => old ? { ...old, [key]: value } : old); setNotice("");
  }
  async function save() {
    if (!draft || !data) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const fresh = await api<Snapshot>("/api/admin/runtime-settings", { method: "PUT",
        body: JSON.stringify({ expected_revision: data.revision, settings: draft }) });
      setData(fresh); setDraft(fresh.settings); setStatus(fresh); setNotice("平台参数已保存，后台进程正在加载。");
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  const numeric: [keyof Options, string, number, number][] = [
    ["max_tokens", "输出 token 上限（含推理）", 1000, 100000],
    ["request_timeout_seconds", "模型请求超时（秒）", 10, 1200],
    ["generation_timeout_seconds", "出题单轮总时限（秒）", 30, 3600],
    ["generation_max_attempts", "出题最大自动轮次", 1, 5],
    ["generation_global_concurrency", "全局出题并发上限", 1, 100],
    ["grading_timeout_seconds", "评分整轮总时限（秒）", 30, 3600],
  ];
  return <section className="card settings-section runtime-settings"><h2>模型与任务参数</h2>
    <p className="muted">平台共用，影响后续领取的任务。进程每 3 秒加载设置；运行中的任务使用原配置。</p>
    {error && <div className="error" role="alert">{error}</div>}{notice && <div className="notice" role="status">{notice}</div>}
    {!data || !draft ? <button className="secondary" disabled={busy} onClick={() => void reload()}>加载运行参数</button> : <>
      <div className="settings-grid">{(["deepseek", "openai"] as Service[]).map(service => <div key={service}>
        <h3>{service === "deepseek" ? "DeepSeek API" : "备用 OpenAI API"}</h3>
        <p className="muted runtime-endpoint">{data.services[service].base_url}</p>
        <p>{data.services[service].configured ? "API Key 已配置" : "API Key 未配置"}</p>
        <label>{service === "deepseek" ? "DeepSeek 模型" : "备用 OpenAI 模型"}<select aria-label={service === "deepseek" ? "DeepSeek 模型" : "备用 OpenAI 模型"} value={draft[`${service}_model`]} onChange={e => change(`${service}_model`, e.target.value)}>
          {data.services[service].models.map(model => <option key={model} value={model}>{model}</option>)}</select></label>
        {service === "openai" && draft.openai_model === "gpt-6.1-sol" && <p className="muted">保留指定模型选项；接入核验时该名称未出现在服务方模型列表中。</p>}
      </div>)}</div>
      <div className="settings-grid">{(["generation_service", "grading_service"] as const).map(key => <label key={key}>{key === "generation_service" ? "出题使用服务" : "评分使用服务"}
        <select aria-label={key === "generation_service" ? "出题使用服务" : "评分使用服务"} value={draft[key]} onChange={e => change(key, e.target.value as Service)}>
          <option value="deepseek">DeepSeek API</option><option value="openai">备用 OpenAI API</option></select></label>)}
        <label>推理强度<select value={draft.reasoning_effort} onChange={e => change("reasoning_effort", e.target.value as Options["reasoning_effort"])}>
          <option value="max">max（充分推理）</option><option value="high">high</option><option value="low">low</option></select><small>OpenAI 的 max 对应 xhigh；DeepSeek 保持 thinking 开启。</small></label>
        {numeric.map(([key, label, min, max]) => <label key={key}>{label}<input type="number" min={min} max={max} step={1} value={draft[key] as number}
          onChange={e => change(key, Number(e.target.value))} /></label>)}
      </div>
      <p className="muted">服务通过设置手动切换。请求超时指网络阶段等待；单轮总时限含排队及重试。HTTP 层最多 5 次请求，出题自动轮次另计。</p>
      <p className="muted">每进程请求并发 {data.runtime.request_concurrency_per_process}，出题执行器 {data.runtime.generation_workers_per_process}，评分执行器 {data.runtime.grading_workers_per_process}；执行器数量由部署配置决定。</p>
      <div className="settings-actions"><button disabled={busy} onClick={() => void save()}>保存平台参数</button>
        <button className="secondary" disabled={busy} onClick={() => { setDraft(data.defaults); setNotice("已恢复初始值，保存后生效。"); }}>恢复运行初始值</button>
        <button className="secondary" disabled={busy} onClick={() => void reload()}>重新加载运行参数</button></div>
      <p className="muted" role="status">{status ? `服务器保存版本 ${status.revision} · ${status.runtime.loaded_workers} / ${status.runtime.active_workers} 个在线进程已加载${status.runtime.active_workers === 0 ? "（尚无运行确认）" : ""}` : "运行状态暂不可用"}</p>
      {status && status.revision !== data.revision && <p className="notice">其他教师已更新平台参数，请重新加载后编辑。</p>}
    </>}
  </section>;
}
