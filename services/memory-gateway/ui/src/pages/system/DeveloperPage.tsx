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
import { CLIENT_MODEL_ID, CLIENT_MODEL_MODE_ALIASES } from "../../utils/constants";
import { copyText } from "../../utils/files";
import { clientConfigText, dateText, errorMessage, joinUrl } from "../../utils/format";
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
      setCreateError("请先保存上一把密钥，再点「我已保存」");
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
      notify("密钥已创建；离开前请立即复制保存", "success");
    } catch (error) {
      setCreateError(errorMessage(error));
    } finally {
      setCreating(false);
    }
  };

  const revoke = async (record: AuthTokenRecord) => {
    const confirmed = await confirm({
      title: "撤销密钥",
      message: (
        <>
          确定撤销「{record.name}」吗？这台设备下一次请求会立即失去访问权限。
          {record.is_current && (
            <strong className="token-current-warning">
              这是当前浏览器登录用的密钥；撤销后需要重新登录。
            </strong>
          )}
        </>
      ),
      tone: "danger",
      confirmLabel: "撤销"
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
      notify(result.already_revoked ? "这把密钥已经撤销" : "密钥已撤销", "success");
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
        subtitle="每个聊天 App 或 MCP 客户端各用一把自己的密钥；哪台设备丢了就只撤销哪一把。"
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
              旧 key 同时拥有聊天、MCP 和登录权限。请先为各设备迁移到下方按用途分开的密钥，
              再在服务配置中关闭 legacy key。本页只显示迁移状态，绝不会回显旧 key。
            </p>
            <details className="provider-bootstrap-help">
              <summary>如何关闭 legacy key？</summary>
              <p>确认所有设备都已换用下方按用途分开的密钥后：</p>
              <p>Docker：在安装目录（默认 memory-platform）打开终端后依次运行：</p>
              <code>docker compose -f docker-compose.user.yml --profile maintenance run --rm stack-maintenance config set GATEWAY_LEGACY_API_KEY_ENABLED false</code>
              <code>docker compose -f docker-compose.user.yml restart memory-gateway</code>
              <p>源码安装：在仓库根目录运行后重启服务：</p>
              <code>scripts/memgw config set GATEWAY_LEGACY_API_KEY_ENABLED false</code>
              <p>关闭后旧 key 立即失效；如仍有设备在用它，会收到 401，需要改用新密钥。</p>
            </details>
          </div>
        </section>
      )}

      <section className="panel access-card token-create-panel">
        <div className="panel-header">
          <div>
            <h2>创建密钥</h2>
            <p className="muted">
              密钥归当前用户 <code>{tokens?.current_user_id || settings.userId}</code> 所有，客户端无法切换到别的用户。
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
              <strong>聊天 App</strong>
              <small>默认推荐。Chatbox、RikkaHub、FLIT 等 OpenAI 兼容客户端填这把聊天密钥。</small>
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
              <strong>MCP 客户端</strong>
              <small>仅供支持 Streamable HTTP MCP 的客户端使用这把 MCP 密钥。</small>
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
            {creating ? "创建中" : role === "chat" ? "创建聊天密钥" : "创建 MCP 密钥"}
          </button>
        </div>
        {createError && <p className="form-error" role="alert">{createError}</p>}

        {created && (
          <div className="one-time-token" role="status" aria-live="polite">
            <div className="one-time-token-heading">
              <div>
                <strong>密钥只显示这一次</strong>
                <p>服务端不保存原文。关闭、刷新或离开此页后无法再次查看，丢了就撤销后重新创建。</p>
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
                {showCreatedToken ? "隐藏密钥" : "显示密钥"}
              </button>
              <button
                className="primary-button"
                type="button"
                onClick={() =>
                  void copy(
                    connectionText(created, chatBaseUrl, mcpUrl),
                    "接入配置已复制；其中包含只显示一次的密钥，请妥善保存"
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
            <p className="muted">聊天 App 填聊天密钥，MCP 客户端填 MCP 密钥；都不要填登录本网页的登录密钥。</p>
          </div>
        </div>
        <div className="integration-guide-grid">
          <article className="integration-guide-card">
            <MessageCircle size={19} />
            <h3>聊天 App（OpenAI 兼容）</h3>
            <FieldList
              compact
              entries={[
                ["Base URL", chatBaseUrl],
                ["API Key", "这台设备的聊天密钥"],
                ["模型", CLIENT_MODEL_ID]
              ]}
            />
            <p className="muted integration-guide-note">
              想让某个对话只读或完全不碰记忆，把模型名换成
              {CLIENT_MODEL_MODE_ALIASES.map((alias) => (
                <span key={alias.id}>
                  {" "}
                  <code>{alias.id}</code>（{alias.label}）
                </span>
              ))}
              即可，不需要自定义 Header；客户端需重新同步模型列表才能看到这些名字。
            </p>
          </article>
          <article className="integration-guide-card">
            <Plug size={19} />
            <h3>Streamable HTTP MCP</h3>
            <FieldList
              compact
              entries={[
                ["地址", mcpUrl],
                ["鉴权", "Bearer + 这台设备的 MCP 密钥"]
              ]}
            />
          </article>
        </div>
      </section>

      <section className="panel access-card">
        <div className="panel-header">
          <div>
            <h2>已创建的密钥</h2>
            <p className="muted">只显示名称和使用状态，不会显示密钥原文。</p>
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
        {loading && !tokens && <LoadingBlock label="读取已创建的密钥" />}
        {loadError && <ErrorBlock message={loadError} onRetry={() => void load()} />}
        {!loading && !loadError && tokens?.data.length === 0 && (
          <EmptyBlock
            label="还没有创建密钥"
            hint="先为聊天 App 创建聊天密钥，或为 MCP 客户端创建 MCP 密钥。"
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
                        这是该用户最后一把可用的登录密钥。请先在运行主机用
                        <code>memgw token create --role console</code> 创建备用密钥。
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
                        ? "必须保留至少一把可用的登录密钥"
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
          以下管理接口只接受登录密钥；聊天密钥和 MCP 密钥无法访问。
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
    return clientConfigText(chatBaseUrl, created.token);
  }
  return [
    `MCP URL: ${mcpUrl}`,
    `Authorization: Bearer ${created.token}`
  ].join("\n");
}


function roleLabel(role: AuthTokenRecord["role"]): string {
  if (role === "chat") return "聊天";
  if (role === "mcp") return "MCP";
  return "登录";
}
