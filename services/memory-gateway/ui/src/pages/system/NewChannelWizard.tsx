import {
  CheckCircle2,
  ClipboardCopy,
  Eye,
  EyeOff,
  PlugZap,
  Save,
  TriangleAlert,
  X
} from "lucide-react";
import { useState } from "react";
import type { MemoryApi } from "../../api";
import { loadSettings } from "../../storage";
import type {
  ModelGatewayCapabilities,
  ModelGatewayControlSnapshot,
  ModelGatewayRouteAssignmentInput
} from "../../types";
import { errorMessage } from "../../utils/format";

type Feedback = { tone: "success" | "warning" | "error"; message: string };

export const ROUTE_LABELS: Record<string, string> = {
  "memory.chat": "日常聊天",
  "memory.extract": "提取长期记忆",
  "memory.compact": "压缩对话上下文",
  "memory.core": "整理核心记忆",
  "memory.review": "记忆体检",
  "knowledge.fast": "快速知识检索",
  "knowledge.pro": "深度知识检索",
  "memory.embedding": "语义搜索",
  "pricing.research": "价格信息研究"
};

// 与 services/model-gateway/model_gateway/quickstart.py 的 CHAT_ROUTES / CHANNEL_PRESETS 保持一致。
const CHAT_ROUTE_IDS = [
  "memory.chat",
  "memory.extract",
  "memory.compact",
  "memory.core",
  "memory.review",
  "knowledge.fast",
  "knowledge.pro"
];
const EMBEDDING_ROUTE_ID = "memory.embedding";

const CHANNEL_PRESETS = [
  {
    id: "deepseek",
    label: "DeepSeek 官方",
    channel_operator: "deepseek",
    base_url: "https://api.deepseek.com",
    adapter: "deepseek"
  },
  {
    id: "kimi-cn",
    label: "Kimi / Moonshot 中国区",
    channel_operator: "moonshot",
    base_url: "https://api.moonshot.cn/v1",
    adapter: "kimi"
  },
  {
    id: "mimo",
    label: "小米 MiMo 官方",
    channel_operator: "xiaomi",
    base_url: "https://api.xiaomimimo.com/v1",
    adapter: "mimo"
  },
  {
    id: "dashscope-cn",
    label: "阿里云百炼 / DashScope 北京区",
    channel_operator: "dashscope",
    base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1",
    adapter: "generic"
  }
] as const;

type PresetId = (typeof CHANNEL_PRESETS)[number]["id"] | "custom";

const ADAPTER_OPTIONS = ["generic", "kimi", "deepseek", "mimo"] as const;

const CAPABILITY_OPTIONS: Array<{
  key: keyof ModelGatewayCapabilities;
  label: string;
}> = [
  { key: "tools", label: "工具调用 tools" },
  { key: "parallel_tools", label: "并行工具 parallel_tools" },
  { key: "reasoning", label: "推理 reasoning" },
  { key: "multimodal_input", label: "多模态输入 multimodal_input" },
  { key: "json_object", label: "JSON 对象 json_object" },
  { key: "json_schema", label: "JSON Schema json_schema" }
];

function suggestEmbeddingSpace(operator: string, model: string, dimensions: string): string {
  const slug = model
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9._:-]+/g, "-")
    .replace(/^[-._:]+|[-._:]+$/g, "");
  if (!slug) return "";
  const owner = operator.trim().toLowerCase() || "channel";
  const dims = dimensions.trim();
  return `${owner}.${slug}${dims ? `:${dims}` : ""}`;
}

export function NewChannelWizard({
  api,
  adminKey,
  control,
  onClose,
  onCompleted
}: {
  api: MemoryApi;
  adminKey: string;
  control: ModelGatewayControlSnapshot;
  onClose: () => void;
  onCompleted: () => Promise<void> | void;
}) {
  const [preset, setPreset] = useState<PresetId>("deepseek");
  const [operator, setOperator] = useState<string>(CHANNEL_PRESETS[0].channel_operator);
  const [baseUrl, setBaseUrl] = useState<string>(CHANNEL_PRESETS[0].base_url);
  const [adapter, setAdapter] = useState<string>(CHANNEL_PRESETS[0].adapter);
  const [apiKey, setApiKey] = useState("");
  const [showApiKey, setShowApiKey] = useState(false);
  const [revision, setRevision] = useState(control.revision);
  const [channelCreated, setChannelCreated] = useState(false);
  const [secretWritten, setSecretWritten] = useState(false);
  const [connectionId, setConnectionId] = useState("");
  const [checkDetail, setCheckDetail] = useState<{ level: string; detail: string } | null>(null);
  const [models, setModels] = useState<string[]>([]);
  const [chatModel, setChatModel] = useState("");
  const [modelAuthor, setModelAuthor] = useState("");
  const [capabilities, setCapabilities] = useState<ModelGatewayCapabilities>({});
  const [embeddingEnabled, setEmbeddingEnabled] = useState(false);
  const [embeddingModel, setEmbeddingModel] = useState("");
  const [embeddingDimensions, setEmbeddingDimensions] = useState("");
  const [embeddingSpace, setEmbeddingSpace] = useState("");
  const [embeddingSpaceEdited, setEmbeddingSpaceEdited] = useState(false);
  const [applyWarnings, setApplyWarnings] = useState<string[]>([]);
  const [busy, setBusy] = useState("");
  const [feedback, setFeedback] = useState<Feedback | null>(null);
  const [done, setDone] = useState(false);

  const hasAdminKey = Boolean(adminKey.trim());
  const channelReady = channelCreated && secretWritten;
  const connectionVerified = channelReady && Boolean(checkDetail && checkDetail.level !== "error");

  const selectPreset = (id: PresetId) => {
    setPreset(id);
    const found = CHANNEL_PRESETS.find((item) => item.id === id);
    if (found) {
      setOperator(found.channel_operator);
      setBaseUrl(found.base_url);
      setAdapter(found.adapter);
    }
    setFeedback(null);
  };

  const toggleCapability = (key: keyof ModelGatewayCapabilities, checked: boolean) => {
    setCapabilities((current) => {
      const next = { ...current, [key]: checked };
      // parallel_tools 依赖 tools，与 quickstart 的处理一致。
      if (key === "parallel_tools" && checked) next.tools = true;
      if (key === "tools" && !checked) next.parallel_tools = false;
      return next;
    });
  };

  const updateEmbeddingModel = (value: string) => {
    setEmbeddingModel(value);
    if (!embeddingSpaceEdited) {
      setEmbeddingSpace(suggestEmbeddingSpace(operator, value, embeddingDimensions));
    }
  };

  const updateEmbeddingDimensions = (value: string) => {
    setEmbeddingDimensions(value);
    if (!embeddingSpaceEdited) {
      setEmbeddingSpace(suggestEmbeddingSpace(operator, embeddingModel, value));
    }
  };

  const applyCheckReport = (report: Awaited<ReturnType<MemoryApi["checkProviderConnection"]>>) => {
    const info = report.connections[0];
    setCheckDetail(info ? { level: info.level, detail: info.detail } : null);
    setModels(info?.discovered_models || []);
    return info;
  };

  const runPhase1 = async () => {
    if (!hasAdminKey || Boolean(busy)) return;
    setBusy("phase1");
    setFeedback(null);
    try {
      let id = connectionId;
      let currentRevision = revision;
      if (!channelCreated) {
        const body = {
          revision: currentRevision,
          channel_operator: operator,
          adapter,
          base_url: baseUrl
        };
        await api.createProviderConnection({ ...body, dry_run: true }, adminKey.trim());
        const applied = await api.createProviderConnection(
          { ...body, dry_run: false },
          adminKey.trim()
        );
        id = applied.connection_id;
        currentRevision = applied.revision;
        setConnectionId(id);
        setRevision(currentRevision);
        setChannelCreated(true);
      }
      const nextApiKey = apiKey.trim();
      if (!secretWritten && !nextApiKey) {
          setFeedback({ tone: "error", message: "请填写渠道 API Key 后再继续。" });
          return;
      }
      // 检查失败后允许直接在向导内粘贴新 key。只要输入框非空，
      // 就先替换再检查；空白则保留已经写入的值。
      if (nextApiKey) {
        await api.updateProviderSecret(id, nextApiKey, adminKey.trim());
        setSecretWritten(true);
        setApiKey("");
      }
      const info = applyCheckReport(await api.checkProviderConnection(id, adminKey.trim()));
      if (info && info.level !== "error") {
        if (info.discovered_models?.length === 1) {
          setChatModel(info.discovered_models[0]);
        }
        setFeedback({
          tone: info.discovered_models?.length ? "success" : "warning",
          message: info.discovered_models?.length
            ? `渠道已创建、密钥已写入，发现 ${info.discovered_models.length} 个可见模型。`
            : "渠道已创建、密钥已写入，但未能解析模型列表；下方仍可手动填写模型 ID。"
        });
      } else {
        setFeedback({
          tone: "error",
          message: `渠道已创建，但连接检查失败：${info?.detail || "未知错误"}。可在下方修正密钥后重新检查。`
        });
      }
    } catch (cause) {
      setFeedback({ tone: "error", message: errorMessage(cause) });
    } finally {
      setBusy("");
    }
  };

  const runPhase2 = async () => {
    if (!hasAdminKey || !connectionVerified || Boolean(busy)) return;
    const chat = chatModel.trim();
    if (!chat) {
      setFeedback({ tone: "error", message: "请填写聊天模型的精确 upstream_model ID。" });
      return;
    }
    const dimensions = Number.parseInt(embeddingDimensions.trim(), 10);
    if (embeddingEnabled) {
      if (!embeddingModel.trim()) {
        setFeedback({ tone: "error", message: "已启用向量模型，请填写 embedding 模型 ID。" });
        return;
      }
      if (!Number.isInteger(dimensions) || dimensions < 1) {
        setFeedback({ tone: "error", message: "向量模型必须填写正整数维度 dimensions。" });
        return;
      }
      if (!embeddingSpace.trim()) {
        setFeedback({ tone: "error", message: "向量模型必须填写 embedding_space（向量空间名称）。" });
        return;
      }
    }
    setBusy("apply");
    setFeedback(null);
    try {
      const deployments = [
        {
          upstream_model: chat,
          model_author: modelAuthor.trim(),
          kind: "chat" as const,
          capabilities: { streaming: true, ...capabilities }
        },
        ...(embeddingEnabled
          ? [
              {
                upstream_model: embeddingModel.trim(),
                model_author: modelAuthor.trim(),
                kind: "embedding" as const,
                capabilities: { streaming: false },
                dimensions,
                embedding_space: embeddingSpace.trim()
              }
            ]
          : [])
      ];
      const routes: ModelGatewayRouteAssignmentInput[] = CHAT_ROUTE_IDS.map((id) => ({
        id,
        kind: "chat" as const,
        targets: ["$0"],
        enabled: true
      }));
      if (embeddingEnabled) {
        routes.push({ id: EMBEDDING_ROUTE_ID, kind: "embedding" as const, targets: ["$1"], enabled: true });
      }
      const body = { revision, connection: connectionId, deployments, routes };
      const preview = await api.applyProviderDeployments(
        { ...body, dry_run: true },
        adminKey.trim()
      );
      if (preview.warnings.length > 0 && applyWarnings.join() !== preview.warnings.join()) {
        setApplyWarnings(preview.warnings);
        setFeedback({
          tone: "warning",
          message: `校验通过但有提醒：${preview.warnings[0]}。再次点击“校验并应用”确认执行。`
        });
        return;
      }
      const applied = await api.applyProviderDeployments(
        { ...body, dry_run: false },
        adminKey.trim()
      );
      await onCompleted();
      setDone(true);
      setFeedback({
        tone: "success",
        message: `已创建 ${applied.deployments.length} 个 deployment，并更新 ${applied.changed_routes.length} 条用途路由；Model Gateway 已热加载。`
      });
    } catch (cause) {
      setFeedback({ tone: "error", message: errorMessage(cause) });
    } finally {
      setBusy("");
    }
  };

  const existingTargets = (routeId: string) =>
    control.routes.find((route) => route.id === routeId)?.targets || [];
  const replacedRoutes = [...CHAT_ROUTE_IDS, ...(embeddingEnabled ? [EMBEDDING_ROUTE_ID] : [])].filter(
    (id) => existingTargets(id).length > 0
  );

  const phase1Disabled =
    !hasAdminKey ||
    Boolean(busy) ||
    done ||
    (!channelCreated && (!operator.trim() || !baseUrl.trim())) ||
    (!secretWritten && !apiKey.trim());

  const copyClientSettings = async () => {
    const settings = loadSettings();
    const baseUrl = `${settings.apiBaseUrl.replace(/\/+$/, "")}/v1`;
    await navigator.clipboard.writeText(
      `Base URL: ${baseUrl}\nAPI Key: ${settings.apiKey}\n模型名: memory-auto`
    );
    setFeedback({ tone: "success", message: "客户端配置已复制；内容包含访问密钥，请妥善保管。" });
  };

  return (
    <section className="panel provider-editor-section provider-wizard" aria-labelledby="new-channel-title">
      <div className="panel-header provider-section-header">
        <div>
          <h2 id="new-channel-title">新建渠道</h2>
          <p>
            选择你购买或注册过的模型服务，粘贴 API Key，再选一个聊天模型。
            普通使用不需要理解路由或 deployment。
          </p>
        </div>
        <button
          type="button"
          className="icon-button"
          onClick={onClose}
          aria-label="关闭新建渠道"
          title="关闭"
        >
          <X size={16} aria-hidden />
        </button>
      </div>

      <div className="provider-wizard-body">
        {feedback && (
          <div
            className={`provider-feedback is-${feedback.tone}`}
            role={feedback.tone === "error" ? "alert" : "status"}
          >
            {feedback.tone === "success" ? (
              <CheckCircle2 size={18} aria-hidden />
            ) : (
              <TriangleAlert size={18} aria-hidden />
            )}
            <span>{feedback.message}</span>
          </div>
        )}

        {done ? (
          <div className="provider-wizard-step">
            <h3><CheckCircle2 size={18} aria-hidden /> 模型配置完成</h3>
            <p className="provider-wizard-hint">现在可以把下面三项填进 Chatbox、RikkaHub、FLIT 等客户端。</p>
            <div className="client-config-summary">
              <span><small>类型</small><strong>OpenAI 兼容 · Chat Completions</strong></span>
              <span><small>Base URL</small><code>{loadSettings().apiBaseUrl.replace(/\/+$/, "")}/v1</code></span>
              <span><small>模型名</small><code>memory-auto</code></span>
            </div>
            <div className="provider-wizard-actions">
              <button type="button" className="secondary-button" onClick={() => void copyClientSettings()}>
                <ClipboardCopy size={16} aria-hidden />
                复制客户端配置
              </button>
              <button type="button" className="primary-button" onClick={onClose}>
                完成
              </button>
            </div>
          </div>
        ) : (
          <>
            <div className="provider-wizard-step">
              <h3>
                <span className="provider-step-index" aria-hidden>1</span>选择渠道
              </h3>
              <div className="provider-preset-grid" role="radiogroup" aria-label="渠道预设">
                {CHANNEL_PRESETS.map((item) => (
                  <button
                    key={item.id}
                    type="button"
                    role="radio"
                    aria-checked={preset === item.id}
                    className={`provider-preset-card${preset === item.id ? " is-active" : ""}`}
                    onClick={() => selectPreset(item.id)}
                    disabled={channelCreated || Boolean(busy)}
                  >
                    <strong>{item.label}</strong>
                    <span>{item.base_url}</span>
                  </button>
                ))}
                <button
                  type="button"
                  role="radio"
                  aria-checked={preset === "custom"}
                  className={`provider-preset-card${preset === "custom" ? " is-active" : ""}`}
                  onClick={() => selectPreset("custom")}
                  disabled={channelCreated || Boolean(busy)}
                >
                  <strong>自定义渠道</strong>
                  <span>填写服务商提供的 OpenAI 兼容地址</span>
                </button>
              </div>
              {preset === "custom" && (
                <div className="provider-field-grid">
                  <label className="field-block">
                    <span>渠道简称</span>
                    <input
                      value={operator}
                      onChange={(event) => setOperator(event.target.value)}
                      spellCheck={false}
                      placeholder="例如 deepseek"
                      disabled={channelCreated || Boolean(busy)}
                    />
                  </label>
                  <label className="field-block">
                    <span>官方 API 地址（远程必须 HTTPS）</span>
                    <input
                      value={baseUrl}
                      onChange={(event) => setBaseUrl(event.target.value)}
                      spellCheck={false}
                      placeholder="https://api.example.com/v1"
                      disabled={channelCreated || Boolean(busy)}
                    />
                  </label>
                  <details className="provider-inline-advanced">
                    <summary>高级：兼容适配器</summary>
                    <label className="field-block">
                      <span>适配器</span>
                      <select
                        value={adapter}
                        onChange={(event) => setAdapter(event.target.value)}
                        disabled={channelCreated || Boolean(busy)}
                      >
                        {ADAPTER_OPTIONS.map((option) => <option key={option} value={option}>{option}</option>)}
                      </select>
                    </label>
                  </details>
                </div>
              )}
            </div>

            <div className="provider-wizard-step">
              <h3>
                <span className="provider-step-index" aria-hidden>2</span>渠道 API Key
              </h3>
              <label className="field-block">
                <span>{secretWritten ? "API Key 已写入；留空表示保持不变" : "API Key（只单向写入，页面不保存、不回显）"}</span>
                <div className="secret-field">
                  <input
                    type={showApiKey ? "text" : "password"}
                    value={apiKey}
                    onChange={(event) => setApiKey(event.target.value)}
                    autoComplete="new-password"
                    spellCheck={false}
                    placeholder={secretWritten ? "仅在需要替换时重新填写" : "sk-..."}
                    disabled={done || Boolean(busy)}
                  />
                  <button
                    type="button"
                    className="icon-button"
                    onClick={() => setShowApiKey((current) => !current)}
                    aria-label={showApiKey ? "隐藏渠道密钥" : "显示渠道密钥"}
                  >
                    {showApiKey ? <EyeOff size={16} aria-hidden /> : <Eye size={16} aria-hidden />}
                  </button>
                </div>
              </label>
            </div>

            <div className="provider-wizard-step">
              <h3>
                <span className="provider-step-index" aria-hidden>3</span>验证渠道
              </h3>
              <p className="provider-wizard-hint">
                将安全保存密钥，并免费读取该账号可见的模型列表；不会发送聊天内容，也不会产生推理费用。
              </p>
              {connectionId && (
                <p className="provider-wizard-result">
                  渠道记录已创建{secretWritten && "，密钥已安全保存"}
                </p>
              )}
              {checkDetail && (
                <div className={`provider-check-result is-${checkDetail.level}`} role="status">
                  {checkDetail.level === "ok" ? (
                    <CheckCircle2 size={15} aria-hidden />
                  ) : (
                    <TriangleAlert size={15} aria-hidden />
                  )}
                  <span>{checkDetail.detail}</span>
                </div>
              )}
              {models.length > 0 && (
                <p className="provider-wizard-hint">该密钥可见 {models.length} 个模型。</p>
              )}
              <div className="provider-wizard-actions">
                <button
                  type="button"
                  className="primary-button"
                  onClick={() => void runPhase1()}
                  disabled={phase1Disabled}
                >
                  <PlugZap size={16} aria-hidden />
                  {busy === "phase1"
                    ? "正在创建并检查"
                    : channelReady
                      ? apiKey.trim() ? "保存新密钥并重新检查" : "重新检查连接"
                      : "保存并检查"}
                </button>
              </div>
            </div>

            {connectionVerified && (
              <>
                <div className="provider-wizard-step">
                  <h3>
                    <span className="provider-step-index" aria-hidden>4</span>选择模型
                  </h3>
                  <div className="provider-field-grid">
                    <label className="field-block">
                      <span>聊天模型</span>
                      {models.length > 0 ? (
                        <select value={chatModel} onChange={(event) => setChatModel(event.target.value)} disabled={Boolean(busy)}>
                          <option value="">请选择一个模型</option>
                          {models.map((model) => <option key={model} value={model}>{model}</option>)}
                        </select>
                      ) : (
                        <input
                          value={chatModel}
                          onChange={(event) => setChatModel(event.target.value)}
                          spellCheck={false}
                          placeholder="填写服务商给出的精确模型名"
                          disabled={Boolean(busy)}
                        />
                      )}
                    </label>
                  </div>
                  <details className="provider-inline-advanced">
                    <summary>高级设置（普通使用无需修改）</summary>
                    <div className="provider-field-grid">
                      <label className="field-block">
                        <span>模型作者标识</span>
                        <input value={modelAuthor} onChange={(event) => setModelAuthor(event.target.value)} spellCheck={false} placeholder={operator || "自动使用渠道简称"} disabled={Boolean(busy)} />
                      </label>
                    </div>
                    <fieldset className="provider-capability-field" disabled={Boolean(busy)}>
                      <legend>只有在服务商明确支持时才勾选</legend>
                      <div className="provider-capability-grid">
                        {CAPABILITY_OPTIONS.map((option) => (
                          <label key={option.key}>
                            <input type="checkbox" checked={Boolean(capabilities[option.key])} onChange={(event) => toggleCapability(option.key, event.target.checked)} />
                            <span>{option.label}</span>
                          </label>
                        ))}
                      </div>
                    </fieldset>
                    <label className="provider-route-toggle provider-embedding-toggle">
                      <input type="checkbox" checked={embeddingEnabled} onChange={(event) => setEmbeddingEnabled(event.target.checked)} disabled={Boolean(busy)} />
                      <span>同时配置向量模型（可选，用于语义搜索）</span>
                    </label>
                    {embeddingEnabled && <div className="provider-field-grid">
                      <label className="field-block">
                        <span>向量模型</span>
                        <input
                          value={embeddingModel}
                          onChange={(event) => updateEmbeddingModel(event.target.value)}
                          list="new-channel-models"
                          spellCheck={false}
                          disabled={Boolean(busy)}
                        />
                      </label>
                      <label className="field-block">
                        <span>向量维度</span>
                        <input
                          value={embeddingDimensions}
                          onChange={(event) => updateEmbeddingDimensions(event.target.value)}
                          inputMode="numeric"
                          spellCheck={false}
                          placeholder="例如 1024"
                          disabled={Boolean(busy)}
                        />
                      </label>
                      <label className="field-block">
                        <span>向量空间名称</span>
                        <input
                          value={embeddingSpace}
                          onChange={(event) => {
                            setEmbeddingSpace(event.target.value);
                            setEmbeddingSpaceEdited(true);
                          }}
                          spellCheck={false}
                          disabled={Boolean(busy)}
                        />
                      </label>
                    </div>}
                  </details>
                </div>

                <div className="provider-wizard-step">
                  <h3>
                    <span className="provider-step-index" aria-hidden>5</span>保存并启用
                  </h3>
                  <p className="provider-wizard-hint">
                    日常聊天、记忆提取和知识检索将使用这个模型。以后可以在高级设置中添加备用模型。
                  </p>
                  {replacedRoutes.length > 0 && (
                    <div className="provider-feedback is-warning" role="status">
                      <TriangleAlert size={18} aria-hidden />
                      <span>
                        以下已有路由的目标将被替换：
                        {replacedRoutes.map((id) => ROUTE_LABELS[id] || id).join("、")}。
                      </span>
                    </div>
                  )}
                  <div className="provider-wizard-actions">
                    <button
                      type="button"
                      className="primary-button"
                      onClick={() => void runPhase2()}
                      disabled={!hasAdminKey || !connectionVerified || !chatModel.trim() || Boolean(busy)}
                    >
                      <Save size={16} aria-hidden />
                      {busy === "apply" ? "正在保存" : "保存并启用"}
                    </button>
                  </div>
                </div>
              </>
            )}
          </>
        )}
      </div>
    </section>
  );
}
