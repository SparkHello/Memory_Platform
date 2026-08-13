import { useCallback, useEffect, useState } from "react";
import {
  CheckCircle2,
  Clipboard,
  Eye,
  EyeOff,
  KeyRound,
  MessageCircle,
  Plug,
  RefreshCcw,
  ShieldAlert,
  Trash2
} from "lucide-react";
import { MemoryApi } from "../../api";
import { FieldList } from "../../components/FormControls";
import { PageHeader } from "../../components/PageHeader";
import { EmptyBlock, ErrorBlock, LoadingBlock } from "../../components/StateBlocks";
import type { ConfirmFn } from "../../hooks/useConfirm";
import type {
  AuthTokenCreateResult,
  AuthTokenListResult,
  AuthTokenRecord,
  ConnectionSettings
} from "../../types";
import { copyText } from "../../utils/files";
import { dateText, errorMessage, joinUrl } from "../../utils/format";
import type { Notify } from "../pageTypes";

type DeviceRole = "chat" | "mcp";

export function DeveloperPage({
  api,
  settings,
  notify,
  confirm
}: {
  api: MemoryApi;
  settings: ConnectionSettings;
  notify: Notify;
  confirm: ConfirmFn;
}) {
  const [tokens, setTokens] = useState<AuthTokenListResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [role, setRole] = useState<DeviceRole>("chat");
  const [memoryAccess, setMemoryAccess] = useState<"read" | "read-write">("read-write");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [created, setCreated] = useState<AuthTokenCreateResult | null>(null);
  // 一次性 token 统一默认掩码，避免旁观屏幕时明文外泄。
  const [showCreatedToken, setShowCreatedToken] = useState(false);
  const [revokingId, setRevokingId] = useState<string | null>(null);

  const chatBaseUrl = joinUrl(settings.apiBaseUrl, "/v1");
  const mcpUrl = joinUrl(settings.apiBaseUrl, "/mcp");

  const load = useCallback(
    async (signal?: AbortSignal) => {
      setLoading(true);
      setLoadError(null);
      try {
        setTokens(await api.authTokens(signal));
      } catch (error) {
        if (signal?.aborted) return;
        setLoadError(errorMessage(error));
      } finally {
        if (!signal?.aborted) setLoading(false);
      }
    },
    [api]
  );

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const copy = async (text: string, message = "已复制") => {
    try {
      await copyText(text);
      notify(message, "success");
    } catch (error) {
      notify(errorMessage(error), "error");
    }
  };

  const createToken = async () => {
    const normalizedName = name.trim();
    if (!normalizedName) {
      setCreateError("请填写能辨认设备或客户端的名称");
      return;
    }
    if (created) {
      setCreateError("请先保存并关闭上一个一次性 token");
      return;
    }
    setCreating(true);
    setCreateError(null);
    try {
      const result = await api.createAuthToken(normalizedName, role, {
        memoryAccess: role === "chat" ? memoryAccess : undefined
      });
      setCreated(result);
      setShowCreatedToken(false);
      setName("");
      setTokens((current) =>
        current
          ? {
              ...current,
              data: [
                ...current.data.filter(
                  (item) => item.token_id !== result.record.token_id
                ),
                result.record
              ]
            }
          : current
      );
      notify("设备 token 已创建；离开前请立即复制保存", "success");
    } catch (error) {
      setCreateError(errorMessage(error));
    } finally {
      setCreating(false);
    }
  };

  const revoke = async (record: AuthTokenRecord) => {
    const confirmed = await confirm({
      title: "撤销设备 token",
      message: (
        <>
          确定撤销「{record.name}」吗？该设备下一次请求会立即失去访问权限。
          {record.is_current && (
            <strong className="token-current-warning">
              这是当前浏览器使用的 token；撤销后需要重新登录。
            </strong>
          )}
        </>
      ),
      tone: "danger",
      confirmLabel: "撤销 token"
    });
    if (!confirmed) return;
    setRevokingId(record.token_id);
    try {
      const result = await api.revokeAuthToken(record.token_id);
      setTokens((current) =>
        current
          ? {
              ...current,
              data: current.data.map((item) =>
                item.token_id === result.record.token_id ? result.record : item
              )
            }
          : current
      );
      notify(result.already_revoked ? "该 token 已经撤销" : "设备 token 已撤销", "success");
    } catch (error) {
      notify(errorMessage(error), "error");
    } finally {
      setRevokingId(null);
    }
  };

  const endpoints = [
    "GET /health",
    "GET /memories",
    "POST /memories/search",
    "GET /memories/core",
    "POST /memories/review",
    "GET /memories/export",
    "GET /knowledge/documents",
    "POST /knowledge/uploads",
    "POST /knowledge/search",
    "POST /knowledge/read",
    "GET /knowledge/export"
  ];

  return (
    <div className="page-stack developer-page">
      <PageHeader
        title="客户端接入"
        subtitle="为每台设备创建最小权限 token，再连接聊天应用或 MCP。"
        showTitle={false}
      />

      {tokens?.legacy_key_enabled && (
        <section className="notice warning legacy-token-warning" role="status">
          <ShieldAlert size={18} />
          <div>
            <strong>
              {tokens.authenticated_with_legacy_key
                ? "当前浏览器仍在使用旧共享访问密钥"
                : "旧共享访问密钥仍处于启用状态"}
            </strong>
            <p>
              旧 key 同时拥有聊天、MCP 和 Console 权限。请先为各设备迁移到下方 scoped
              token，再在服务配置中关闭 legacy key。本页只显示迁移状态，绝不会回显旧 key。
            </p>
            <details className="provider-bootstrap-help">
              <summary>如何关闭 legacy key？</summary>
              <p>确认所有设备都已换用下方 scoped token 后：</p>
              <p>Docker：在安装目录（默认 memory-platform）打开终端后依次运行：</p>
              <code>docker compose -f docker-compose.user.yml --profile maintenance run --rm stack-maintenance config set GATEWAY_LEGACY_API_KEY_ENABLED false</code>
              <code>docker compose -f docker-compose.user.yml restart memory-gateway</code>
              <p>源码安装：在仓库根目录运行后重启服务：</p>
              <code>scripts/memgw config set GATEWAY_LEGACY_API_KEY_ENABLED false</code>
              <p>关闭后旧 key 立即失效；如仍有设备在用它，会收到 401，需要改配 scoped token。</p>
            </details>
          </div>
        </section>
      )}

      <section className="panel access-card token-create-panel">
        <div className="panel-header">
          <div>
            <h2>创建设备 token</h2>
            <p className="muted">
              新 token 固定到当前用户 <code>{tokens?.current_user_id || settings.userId}</code>，
              不能通过请求头切换用户。
            </p>
          </div>
          <KeyRound size={20} />
        </div>

        <div className="token-role-guide" aria-label="接入方式说明">
          <button
            className={`token-role-card ${role === "chat" ? "selected" : ""}`}
            type="button"
            aria-pressed={role === "chat"}
            onClick={() => setRole("chat")}
            disabled={Boolean(created)}
          >
            <MessageCircle size={20} />
            <span>
              <strong>OpenAI 兼容聊天</strong>
              <small>默认推荐。Chatbox、RikkaHub、FLIT 等客户端使用 chat token。</small>
            </span>
          </button>
          <button
            className={`token-role-card ${role === "mcp" ? "selected" : ""}`}
            type="button"
            aria-pressed={role === "mcp"}
            onClick={() => setRole("mcp")}
            disabled={Boolean(created)}
          >
            <Plug size={20} />
            <span>
              <strong>MCP 工具接入</strong>
              <small>仅供支持 Streamable HTTP MCP 的客户端使用 mcp token。</small>
            </span>
          </button>
        </div>

        <div className="token-create-form">
          <label className="field-block">
            <span>设备或客户端名称</span>
            <input
              value={name}
              maxLength={100}
              placeholder={role === "chat" ? "例如：客厅 Mac 的 Chatbox" : "例如：工作电脑的 MCP"}
              onChange={(event) => setName(event.target.value)}
              disabled={creating || Boolean(created)}
            />
          </label>
          {role === "chat" && (
            <label className="field-block">
              <span>记忆写入权限</span>
              <select
                value={memoryAccess}
                onChange={(event) =>
                  setMemoryAccess(event.target.value as "read" | "read-write")
                }
                disabled={creating || Boolean(created)}
              >
                <option value="read-write">读写（默认：自动召回 + 回答后提取）</option>
                <option value="read">只读（可召回，禁止自动写入记忆）</option>
              </select>
              <small className="muted">
                只读 token 适合演示机或共享设备：仍可聊天并注入已有记忆，但不会因对话新增记忆。
              </small>
            </label>
          )}
          <button
            className="primary-button"
            type="button"
            onClick={() => void createToken()}
            disabled={creating || Boolean(created)}
          >
            {creating ? <RefreshCcw className="spin" size={16} /> : <KeyRound size={16} />}
            {creating ? "创建中" : `创建 ${role} token`}
          </button>
        </div>
        {createError && <p className="form-error" role="alert">{createError}</p>}

        {created && (
          <div className="one-time-token" role="status" aria-live="polite">
            <div className="one-time-token-heading">
              <div>
                <strong>只显示这一次</strong>
                <p>服务端只保存哈希。关闭、刷新或离开此页后无法再次查看。</p>
              </div>
            </div>
            <code className="one-time-token-value">
              {showCreatedToken
                ? created.token
                : `${created.token.slice(0, 12)}…${created.token.slice(-4)}`}
            </code>
            <div className="button-row">
              <button
                className="secondary-button"
                type="button"
                onClick={() => setShowCreatedToken((value) => !value)}
              >
                {showCreatedToken ? <EyeOff size={16} /> : <Eye size={16} />}
                {showCreatedToken ? "隐藏 token" : "显示 token"}
              </button>
              <button
                className="primary-button"
                type="button"
                onClick={() =>
                  void copy(
                    connectionText(created, chatBaseUrl, mcpUrl),
                    "接入配置已复制；其中包含一次性 token，请妥善保存"
                  )
                }
              >
                <Clipboard size={16} />
                复制完整接入配置
              </button>
              <button
                className="secondary-button"
                type="button"
                onClick={() => {
                  setCreated(null);
                  setShowCreatedToken(false);
                }}
              >
                <CheckCircle2 size={16} />
                我已保存
              </button>
            </div>
          </div>
        )}
      </section>

      <section className="panel access-card">
        <div className="panel-header">
          <div>
            <h2>接入参数</h2>
            <p className="muted">客户端只使用与入口匹配的 token，不要填 Console 登录 token。</p>
          </div>
        </div>
        <div className="integration-guide-grid">
          <article className="integration-guide-card">
            <MessageCircle size={19} />
            <h3>OpenAI 兼容客户端</h3>
            <FieldList
              compact
              entries={[
                ["Base URL", chatBaseUrl],
                ["API Key", "使用该设备的 chat token"],
                ["模型", "memory-auto"]
              ]}
            />
          </article>
          <article className="integration-guide-card">
            <Plug size={19} />
            <h3>Streamable HTTP MCP</h3>
            <FieldList
              compact
              entries={[
                ["地址", mcpUrl],
                ["鉴权", "Bearer + 该设备的 mcp token"]
              ]}
            />
          </article>
        </div>
      </section>

      <section className="panel access-card">
        <div className="panel-header">
          <div>
            <h2>已创建设备</h2>
            <p className="muted">这里只显示标识和使用状态，不保存或回显 token 原文。</p>
          </div>
          <button
            className="secondary-button compact"
            type="button"
            onClick={() => void load()}
            disabled={loading}
          >
            <RefreshCcw size={15} className={loading ? "spin" : ""} />
            刷新
          </button>
        </div>
        {loading && !tokens && <LoadingBlock label="读取设备 token" />}
        {loadError && <ErrorBlock message={loadError} onRetry={() => void load()} />}
        {!loading && !loadError && tokens?.data.length === 0 && (
          <EmptyBlock
            label="还没有 scoped token"
            hint="先为聊天客户端创建 chat token，或为 MCP 客户端创建 mcp token。"
          />
        )}
        {tokens && tokens.data.length > 0 && (
          <div className="device-token-list">
            {[...tokens.data]
              .sort((left, right) => right.created_at.localeCompare(left.created_at))
              .map((record) => (
                <article
                  className={`device-token-row ${record.revoked_at ? "revoked" : ""}`}
                  key={record.token_id}
                >
                  <div className="device-token-main">
                    <strong>{record.name}</strong>
                    <div className="device-token-tags">
                      <span className={`token-role-pill role-${record.role}`}>{roleLabel(record.role)}</span>
                      {record.role === "chat" && (
                        <span className="token-role-pill">
                          {record.memory_access === "read" ? "记忆只读" : "记忆读写"}
                        </span>
                      )}
                      <span className={`token-status-pill ${record.revoked_at ? "revoked" : "active"}`}>
                        {record.revoked_at ? "已撤销" : "可用"}
                      </span>
                      {record.is_current && <span className="token-status-pill current">当前登录</span>}
                    </div>
                    <small>ID {record.token_id}</small>
                    {record.revoke_block_reason === "last_active_console_token" && (
                      <small className="token-revoke-blocked" role="status">
                        这是该用户最后一个可用 Console token。请先在运行主机用
                        <code>memgw token create --role console</code> 创建备用凭据。
                      </small>
                    )}
                  </div>
                  <dl className="device-token-meta">
                    <div>
                      <dt>最近使用</dt>
                      <dd>{dateText(record.last_used_at)}</dd>
                    </div>
                    <div>
                      <dt>{record.revoked_at ? "撤销时间" : "创建时间"}</dt>
                      <dd>{dateText(record.revoked_at || record.created_at)}</dd>
                    </div>
                  </dl>
                  <button
                    className="danger-button compact"
                    type="button"
                    onClick={() => void revoke(record)}
                    disabled={
                      record.can_revoke === false ||
                      Boolean(record.revoked_at) ||
                      revokingId === record.token_id ||
                      created?.record.token_id === record.token_id
                    }
                    title={
                      record.revoke_block_reason === "last_active_console_token"
                        ? "必须保留至少一个可用的 Console token"
                        : undefined
                    }
                  >
                    {revokingId === record.token_id ? (
                      <RefreshCcw className="spin" size={15} />
                    ) : (
                      <Trash2 size={15} />
                    )}
                    {record.revoked_at
                      ? "已撤销"
                      : record.revoke_block_reason === "last_active_console_token"
                        ? "需保留"
                      : created?.record.token_id === record.token_id
                        ? "先保存"
                        : "撤销"}
                  </button>
                </article>
              ))}
          </div>
        )}
      </section>

      <section className="panel access-card">
        <div className="panel-header">
          <h2>Console REST</h2>
          <button
            className="secondary-button"
            type="button"
            onClick={() => void copy(endpoints.join("\n"), "常用 Console REST 端点已复制")}
          >
            <Clipboard size={16} />
            复制端点
          </button>
        </div>
        <p className="muted">
          以下管理接口只接受 Console 登录凭证；chat/mcp token 无法访问。
        </p>
        <div className="endpoint-list">
          {endpoints.map((endpoint) => (
            <code key={endpoint}>{endpoint}</code>
          ))}
        </div>
      </section>
    </div>
  );
}


function connectionText(
  created: AuthTokenCreateResult,
  chatBaseUrl: string,
  mcpUrl: string
): string {
  if (created.record.role === "chat") {
    return [
      `Base URL: ${chatBaseUrl}`,
      `API Key: ${created.token}`,
      "Model: memory-auto"
    ].join("\n");
  }
  return [
    `MCP URL: ${mcpUrl}`,
    `Authorization: Bearer ${created.token}`
  ].join("\n");
}


function roleLabel(role: AuthTokenRecord["role"]): string {
  if (role === "chat") return "聊天";
  if (role === "mcp") return "MCP";
  return "Console";
}
