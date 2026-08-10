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
  const onboarding = !settings.apiKey;

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
        eyebrow={onboarding ? "首次设置 · 第 1 步" : undefined}
        title={onboarding ? "输入客户端访问密钥" : "设置"}
        subtitle={onboarding ? "粘贴安装器写入 credentials/gateway.key 的初始 Console token；它只保存在当前浏览器。" : "连接信息与本机偏好。"}
      />
      <section className="panel settings-panel">
        <div className="panel-header">
          <div>
            <span className="panel-kicker">连接</span>
            <h2>连接设置</h2>
          </div>
        </div>
        {onboarding && (
          <div className="notice">已检测到本地服务：<code>{window.location.origin}</code></div>
        )}
        <label className="field-block">
          <span>访问密钥</span>
          <div className="secret-field">
            <input
              type={showKey ? "text" : "password"}
              value={form.apiKey}
              onChange={(event) => setForm({ ...form, apiKey: event.target.value })}
              placeholder="mgw_…（迁移旧安装时也可暂用 legacy key）"
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
        {!onboarding && (
          <details className="settings-advanced-connection">
            <summary>高级连接设置</summary>
            <label className="field-block">
              <span>服务地址</span>
              <input
                value={form.apiBaseUrl}
                onChange={(event) => setForm({ ...form, apiBaseUrl: event.target.value })}
                placeholder={window.location.origin}
              />
            </label>
            <label className="field-block">
              <span>用户 ID</span>
              <input
                value={form.userId}
                onChange={(event) => setForm({ ...form, userId: event.target.value })}
                placeholder="default"
              />
              <small>默认凭证绑定到 default。只有管理员明确开启多用户头时才修改。</small>
            </label>
          </details>
        )}
        <div className="button-row">
          {!onboarding && (
            <button className="secondary-button" type="button" onClick={() => onSave(form)}>
              仅保存
            </button>
          )}
          <button
            className="primary-button"
            type="button"
            disabled={testing || !form.apiKey.trim()}
            onClick={testConnection}
          >
            {testing ? "正在验证" : onboarding ? "验证并继续" : "测试并保存"}
          </button>
        </div>
        <div className="connection-checks" aria-live="polite">
          <ConnectionCheck icon={Server} label="服务在线" state={serviceCheck} />
          <ConnectionCheck icon={KeyRound} label="密钥鉴权有效" state={authCheck} />
          <p>{testMessage}</p>
        </div>
      </section>

      {!onboarding && (
        <details className="panel panel--quiet settings-reference settings-reference-details">
          <summary>高级：运维与旧版直连配置参考</summary>
          <div className="notice">
            日常模型配置请使用「模型与路由」，设备 token 请使用「接入信息」。下面内容仅供源码运维或迁移旧版 direct 配置时排障。
          </div>
          <h2>服务进程管理</h2>
          <div className="config-grid">
            <div className="config-item">
              <code>scripts/memgw stack status</code>
              <span>源码安装：在仓库目录查看两个服务状态；start / stop / restart 控制启停</span>
            </div>
            <div className="config-item">
              <code>docker compose -f docker-compose.user.yml ps</code>
              <span>Docker 安装：在 compose 文件目录查看状态；Docker Desktop 可设置登录时启动</span>
            </div>
          </div>
          <h2>旧版 direct 环境变量</h2>
          <div className="config-grid">
            {CONFIG_KEYS.map((key) => (
              <div className="config-item" key={key}>
                <code>{key}</code>
                <span>{CONFIG_KEY_HINTS[key]}</span>
              </div>
            ))}
          </div>
        </details>
      )}
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
