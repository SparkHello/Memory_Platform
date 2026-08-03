import { useEffect, useRef, useState } from "react";
import { CheckCircle2, Eye, EyeOff, KeyRound, LoaderCircle, Server, XCircle } from "lucide-react";
import { MemoryApi, isAbortError } from "../../api";
import { normalizeBaseUrl } from "../../storage";
import type { ConnectionSettings } from "../../types";
import { PageHeader } from "../../components/PageHeader";
import { CONFIG_KEYS, CONFIG_KEY_HINTS } from "../../utils/constants";
import { errorMessage } from "../../utils/format";
import type { Notify } from "../pageTypes";

export function SettingsPage({
  settings,
  onSave,
  notify
}: {
  settings: ConnectionSettings;
  onSave: (settings: ConnectionSettings, message?: string) => void;
  notify: Notify;
}) {
  const [form, setForm] = useState(settings);
  const [showKey, setShowKey] = useState(false);
  const [testing, setTesting] = useState(false);
  const [serviceCheck, setServiceCheck] = useState<"idle" | "checking" | "ok" | "error">("idle");
  const [authCheck, setAuthCheck] = useState<"idle" | "checking" | "ok" | "error">("idle");
  const [testMessage, setTestMessage] = useState("保存前可先确认服务和访问密钥都有效。");
  const testRequestRef = useRef<AbortController | null>(null);

  useEffect(() => {
    setForm(settings);
  }, [settings]);

  // 卸载时取消在途的连接测试请求。
  useEffect(() => () => testRequestRef.current?.abort(), []);

  const testConnection = async () => {
    let serviceOnline = false;
    testRequestRef.current?.abort();
    const controller = new AbortController();
    testRequestRef.current = controller;
    setTesting(true);
    setServiceCheck("checking");
    setAuthCheck("idle");
    setTestMessage("正在检查服务…");
    try {
      const client = new MemoryApi({
        ...form,
        apiBaseUrl: normalizeBaseUrl(form.apiBaseUrl),
        userId: form.userId || "default"
      });
      await client.health(controller.signal);
      serviceOnline = true;
      setServiceCheck("ok");
      setAuthCheck("checking");
      setTestMessage("服务在线，正在验证访问密钥…");
      await client.memoryReport(controller.signal);
      setAuthCheck("ok");
      setTestMessage("服务与访问密钥均有效，连接信息已保存。");
      onSave(form, "连接测试通过并已保存");
    } catch (error) {
      if (isAbortError(error)) return;
      if (!serviceOnline) {
        setServiceCheck("error");
      } else {
        setAuthCheck("error");
      }
      const message = errorMessage(error);
      setTestMessage(message);
      notify(message, "error");
    } finally {
      if (testRequestRef.current === controller) setTesting(false);
    }
  };

  return (
    <div className={`page-stack settings-page ${!settings.apiKey ? "onboarding-page" : ""}`}>
      <PageHeader
        eyebrow={!settings.apiKey ? "欢迎使用 Memory Console" : undefined}
        title={!settings.apiKey ? "连接你的本地记忆服务" : "设置"}
        subtitle={!settings.apiKey ? "信息只保存在当前浏览器，不会被上传。" : "连接信息与本机偏好。"}
        action={
          <button className="primary-button" type="button" onClick={() => onSave(form)}>
            保存设置
          </button>
        }
      />
      <section className="panel settings-panel">
        <div className="panel-header">
          <div>
            <span className="panel-kicker">连接</span>
            <h2>连接设置</h2>
          </div>
        </div>
        <label className="field-block">
          <span>服务地址</span>
          <input
            value={form.apiBaseUrl}
            onChange={(event) => setForm({ ...form, apiBaseUrl: event.target.value })}
            placeholder={window.location.origin}
          />
        </label>
        <label className="field-block">
          <span>访问密钥</span>
          <div className="secret-field">
            <input
              type={showKey ? "text" : "password"}
              value={form.apiKey}
              onChange={(event) => setForm({ ...form, apiKey: event.target.value })}
              placeholder="GATEWAY_API_KEY"
            />
            <button
              className="icon-button"
              type="button"
              onClick={() => setShowKey(!showKey)}
              title={showKey ? "隐藏访问密钥" : "显示访问密钥"}
            >
              {showKey ? <EyeOff size={16} /> : <Eye size={16} />}
            </button>
          </div>
        </label>
        <label className="field-block">
          <span>用户 ID</span>
          <input
            value={form.userId}
            onChange={(event) => setForm({ ...form, userId: event.target.value })}
            placeholder="default"
          />
        </label>
        <div className="button-row">
          <button className="primary-button" type="button" onClick={() => onSave(form)}>
            保存到本机浏览器
          </button>
          <button
            className="secondary-button"
            type="button"
            disabled={testing}
            onClick={testConnection}
          >
            测试连接
          </button>
        </div>
        <div className="connection-checks" aria-live="polite">
          <ConnectionCheck icon={Server} label="服务在线" state={serviceCheck} />
          <ConnectionCheck icon={KeyRound} label="密钥鉴权有效" state={authCheck} />
          <p>{testMessage}</p>
        </div>
      </section>

      <section className="panel panel--quiet settings-reference">
        <div className="panel-header">
          <h2>项目配置说明</h2>
        </div>
        <div className="notice">
          当前版本设置页只保存 UI 连接信息；服务端 .env 修改将在后续版本实现。
        </div>
        <div className="config-grid">
          {CONFIG_KEYS.map((key) => (
            <div className="config-item" key={key}>
              <code>{key}</code>
              <span>{CONFIG_KEY_HINTS[key]}</span>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}

function ConnectionCheck({
  icon: Icon,
  label,
  state
}: {
  icon: typeof Server;
  label: string;
  state: "idle" | "checking" | "ok" | "error";
}) {
  const StateIcon = state === "checking" ? LoaderCircle : state === "ok" ? CheckCircle2 : state === "error" ? XCircle : null;
  return (
    <div className={`connection-check state-${state}`}>
      <Icon size={17} />
      <span>{label}</span>
      {StateIcon && <StateIcon className={state === "checking" ? "spin" : ""} size={17} />}
      {!StateIcon && <small>待检查</small>}
    </div>
  );
}

