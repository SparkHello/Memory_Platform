import {
  CheckCircle2,
  ClipboardCopy,
  Eye,
  EyeOff,
  PlugZap,
  Save,
  ShieldCheck,
  TriangleAlert,
  X
} from "lucide-react";
import { useMemo, useState } from "react";
import type { MemoryApi } from "../../api";
import { loadSettings } from "../../storage";
import type {
  ModelGatewayAdapter,
  ModelGatewayCapabilities,
  ModelGatewayChannelBundleBody,
  ModelGatewayChannelDiscoverResult,
  ModelGatewayControlSnapshot,
  ModelGatewayFallbackScope,
  ModelGatewayPlan,
  ModelGatewayRouteAssignmentInput,
  ModelGatewayRouteInfo,
  ModelGatewayRouteOperation,
  ModelGatewayUsageScope
} from "../../types";
import { copyText } from "../../utils/files";
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

const CHAT_ROUTE_IDS = [
  "memory.chat",
  "memory.extract",
  "memory.compact",
  "memory.core",
  "memory.review",
  "knowledge.fast",
  "knowledge.pro"
] as const;
const EMBEDDING_ROUTE_ID = "memory.embedding";

const CHANNEL_PRESETS = [
  {
    id: "deepseek",
    label: "DeepSeek 官方",
    channel_operator: "deepseek",
    base_url: "https://api.deepseek.com",
    adapter: "deepseek",
    plan: "payg",
    usage_scope: "backend_allowed"
  },
  {
    id: "kimi-code",
    label: "Kimi Code 订阅",
    channel_operator: "kimi-code",
    base_url: "https://api.kimi.com/coding/v1",
    adapter: "kimi",
    plan: "coding_plan",
    usage_scope: "interactive_only"
  },
  {
    id: "dashscope-cn",
    label: "阿里云百炼按量",
    channel_operator: "dashscope",
    base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1",
    adapter: "dashscope_openai",
    plan: "payg",
    usage_scope: "backend_allowed"
  },
  {
    id: "dashscope-token-plan",
    label: "阿里云 Token Plan",
    channel_operator: "dashscope-token-plan",
    base_url: "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
    adapter: "dashscope_openai",
    plan: "token_plan",
    usage_scope: "interactive_only"
  }
] as const satisfies ReadonlyArray<{
  id: string;
  label: string;
  channel_operator: string;
  base_url: string;
  adapter: ModelGatewayAdapter;
  plan: ModelGatewayPlan;
  usage_scope: ModelGatewayUsageScope;
}>;

type PresetId = (typeof CHANNEL_PRESETS)[number]["id"] | "custom";

const ADAPTER_OPTIONS: ModelGatewayAdapter[] = [
  "generic",
  "kimi",
  "deepseek",
  "mimo",
  "dashscope_openai"
];
const PLAN_OPTIONS: Array<{ value: ModelGatewayPlan; label: string }> = [
  { value: "payg", label: "按量计费" },
  { value: "subscription", label: "普通订阅" },
  { value: "free_tier", label: "免费额度" },
  { value: "token_plan", label: "Token Plan" },
  { value: "coding_plan", label: "Coding Plan" },
  { value: "direct_tool_only", label: "仅官方工具直连" },
  { value: "custom", label: "自定义" }
];

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

function routePolicy(
  route: ModelGatewayRouteInfo | undefined,
  operation: ModelGatewayRouteOperation
): Pick<ModelGatewayRouteAssignmentInput, "fallback_scope" | "max_attempts" | "enabled"> {
  if (!route || operation === "replace") {
    return { fallback_scope: "none", max_attempts: 1, enabled: true };
  }
  return {
    fallback_scope: route.fallback_scope || "none",
    max_attempts: route.max_attempts || 1,
    enabled: route.enabled
  };
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
  const initialPreset = CHANNEL_PRESETS[0];
  const [preset, setPreset] = useState<PresetId>(initialPreset.id);
  const [operator, setOperator] = useState<string>(initialPreset.channel_operator);
  const [baseUrl, setBaseUrl] = useState<string>(initialPreset.base_url);
  const [adapter, setAdapter] = useState<ModelGatewayAdapter>(initialPreset.adapter);
  const [plan, setPlan] = useState<ModelGatewayPlan>(initialPreset.plan);
  const [usageScope, setUsageScope] = useState<ModelGatewayUsageScope>(initialPreset.usage_scope);
  const [authType, setAuthType] = useState<"bearer" | "x-api-key">("bearer");
  const [allowedPrivateNetworks, setAllowedPrivateNetworks] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [showApiKey, setShowApiKey] = useState(false);
  const [discovery, setDiscovery] = useState<ModelGatewayChannelDiscoverResult | null>(null);
  const [chatModel, setChatModel] = useState("");
  const [modelAuthor, setModelAuthor] = useState("");
  const [adapterProfile, setAdapterProfile] = useState<"inherit" | "dashscope_deepseek_v4">("inherit");
  const [reasoningDefault, setReasoningDefault] = useState<"inherit" | "enabled" | "disabled">("inherit");
  const [capabilities, setCapabilities] = useState<ModelGatewayCapabilities>({});
  const [chatRouteOperation, setChatRouteOperation] = useState<ModelGatewayRouteOperation>("keep");
  const [embeddingEnabled, setEmbeddingEnabled] = useState(false);
  const [embeddingModel, setEmbeddingModel] = useState("");
  const [embeddingDimensions, setEmbeddingDimensions] = useState("");
  const [embeddingSpace, setEmbeddingSpace] = useState("");
  const [embeddingSpaceEdited, setEmbeddingSpaceEdited] = useState(false);
  const [embeddingRouteOperation, setEmbeddingRouteOperation] = useState<ModelGatewayRouteOperation>("keep");
  const [validated, setValidated] = useState(false);
  const [busy, setBusy] = useState<"" | "discover" | "validate" | "apply">("");
  const [feedback, setFeedback] = useState<Feedback | null>(null);
  const [done, setDone] = useState(false);
  const [appliedSummary, setAppliedSummary] = useState({ deployments: 0, routes: 0 });

  const hasAdminKey = Boolean(adminKey.trim());
  const models = discovery?.models || [];
  const discoveryCheck = discovery?.report.connections[0];
  const canAssignBackendRoutes = usageScope === "backend_allowed";
  const embeddingRoute = control.routes.find((route) => route.id === EMBEDDING_ROUTE_ID);
  const currentEmbedding = embeddingRoute?.targets[0]
    ? control.deployments.find((deployment) => deployment.id === embeddingRoute.targets[0])
    : undefined;

  const missingChatRoutes = useMemo(
    () => CHAT_ROUTE_IDS.filter((id) => !control.routes.some((route) => route.id === id)),
    [control.routes]
  );

  const invalidateBundle = () => {
    setValidated(false);
    setFeedback(null);
  };

  const invalidateDiscovery = () => {
    setDiscovery(null);
    setChatModel("");
    invalidateBundle();
  };

  const selectPreset = (id: PresetId) => {
    setPreset(id);
    const found = CHANNEL_PRESETS.find((item) => item.id === id);
    if (found) {
      setOperator(found.channel_operator);
      setBaseUrl(found.base_url);
      setAdapter(found.adapter);
      setPlan(found.plan);
      setUsageScope(found.usage_scope);
    }
    invalidateDiscovery();
  };

  const toggleCapability = (key: keyof ModelGatewayCapabilities, checked: boolean) => {
    setCapabilities((current) => {
      const next = { ...current, [key]: checked };
      if (key === "parallel_tools" && checked) next.tools = true;
      if (key === "tools" && !checked) next.parallel_tools = false;
      return next;
    });
    invalidateBundle();
  };

  const updateEmbeddingModel = (value: string) => {
    setEmbeddingModel(value);
    if (!embeddingSpaceEdited) {
      setEmbeddingSpace(suggestEmbeddingSpace(operator, value, embeddingDimensions));
    }
    invalidateBundle();
  };

  const updateEmbeddingDimensions = (value: string) => {
    setEmbeddingDimensions(value);
    if (!embeddingSpaceEdited) {
      setEmbeddingSpace(suggestEmbeddingSpace(operator, embeddingModel, value));
    }
    invalidateBundle();
  };

  const discover = async () => {
    if (!hasAdminKey || busy || !operator.trim() || !baseUrl.trim() || !apiKey.trim()) return;
    setBusy("discover");
    setFeedback(null);
    setValidated(false);
    try {
      const result = await api.discoverProviderChannel(
        {
          revision: control.revision,
          candidate_key: apiKey.trim(),
          channel_operator: operator.trim(),
          base_url: baseUrl.trim(),
          adapter,
          auth_type: authType,
          allowed_private_networks: allowedPrivateNetworks
            .split(",")
            .map((item) => item.trim())
            .filter(Boolean),
          models_endpoint: "/models"
        },
        adminKey.trim()
      );
      if (result.persisted !== false) {
        throw new Error("模型发现响应没有确认零落盘，已停止后续配置");
      }
      setDiscovery(result);
      if (result.models.length === 1) setChatModel(result.models[0].id);
      setFeedback({
        tone: result.models.length ? "success" : "warning",
        message: result.models.length
          ? `只读检查通过，发现 ${result.models.length} 个模型；候选渠道和密钥尚未保存。`
          : "只读检查通过，但没有返回模型列表；可以手动填写精确模型 ID。候选配置尚未保存。"
      });
    } catch (cause) {
      setDiscovery(null);
      setFeedback({
        tone: "error",
        message: `${errorMessage(cause)}；候选渠道、密钥和 deployment 均未保存。`
      });
    } finally {
      setBusy("");
    }
  };

  const buildRoutes = (): ModelGatewayRouteAssignmentInput[] => {
    const chatRoutes = CHAT_ROUTE_IDS.map((id) => {
      const current = control.routes.find((route) => route.id === id);
      const operation: ModelGatewayRouteOperation = !canAssignBackendRoutes
        ? "keep"
        : current
          ? chatRouteOperation
          : "replace";
      return {
        id,
        operation,
        kind: "chat" as const,
        targets: operation === "keep" ? [] : ["$0"],
        ...routePolicy(current, operation)
      };
    });
    if (!embeddingEnabled) return chatRoutes;
    const operation: ModelGatewayRouteOperation = !canAssignBackendRoutes
      ? "keep"
      : embeddingRoute
        ? embeddingRouteOperation
        : "replace";
    return [
      ...chatRoutes,
      {
        id: EMBEDDING_ROUTE_ID,
        operation,
        kind: "embedding",
        targets: operation === "keep" ? [] : ["$1"],
        ...routePolicy(embeddingRoute, operation)
      }
    ];
  };

  const buildBundle = (): ModelGatewayChannelBundleBody | null => {
    const chat = chatModel.trim();
    if (!chat || !apiKey.trim()) return null;
    const dimensions = Number.parseInt(embeddingDimensions.trim(), 10);
    if (
      embeddingEnabled &&
      (!embeddingModel.trim() ||
        !Number.isInteger(dimensions) ||
        dimensions < 1 ||
        !embeddingSpace.trim())
    ) {
      return null;
    }
    return {
      revision: control.revision,
      connection: {
        channel_operator: operator.trim(),
        adapter,
        base_url: baseUrl.trim(),
        secret: apiKey.trim(),
        auth_type: authType,
        plan,
        usage_scope: usageScope,
        allowed_private_networks: allowedPrivateNetworks
          .split(",")
          .map((item) => item.trim())
          .filter(Boolean)
      },
      deployments: [
        {
          upstream_model: chat,
          model_author: modelAuthor.trim() || "unknown",
          kind: "chat",
          adapter_profile: adapterProfile,
          reasoning_default: reasoningDefault,
          capabilities: { streaming: true, ...capabilities }
        },
        ...(embeddingEnabled
          ? [
              {
                upstream_model: embeddingModel.trim(),
                model_author: modelAuthor.trim() || "unknown",
                kind: "embedding" as const,
                capabilities: { streaming: false },
                dimensions,
                embedding_space: embeddingSpace.trim()
              }
            ]
          : [])
      ],
      routes: buildRoutes()
    };
  };

  const validate = async () => {
    if (!discovery || busy) return;
    const bundle = buildBundle();
    if (!bundle) {
      setFeedback({
        tone: "error",
        message: embeddingEnabled
          ? "请填写聊天模型，以及完整的向量模型、正整数维度和向量空间。"
          : "请填写聊天模型的精确 ID。"
      });
      return;
    }
    setBusy("validate");
    setFeedback(null);
    try {
      const result = await api.validateProviderChannelBundle(bundle, adminKey.trim());
      setValidated(true);
      setFeedback({
        tone: "success",
        message: `完整配置校验通过：将创建 ${result.deployment_ids.length} 个 deployment，变更 ${result.changed_routes.length} 条路由。仍未写入任何配置或密钥。`
      });
    } catch (cause) {
      setValidated(false);
      setFeedback({
        tone: "error",
        message: `${errorMessage(cause)}；完整 bundle 未落盘，现有配置保持不变。`
      });
    } finally {
      setBusy("");
    }
  };

  const apply = async () => {
    if (!validated || busy) return;
    const bundle = buildBundle();
    if (!bundle) {
      setValidated(false);
      return;
    }
    setBusy("apply");
    setFeedback(null);
    try {
      const result = await api.applyProviderChannelBundle(bundle, adminKey.trim());
      setApiKey("");
      setAppliedSummary({
        deployments: result.deployment_ids.length,
        routes: result.changed_routes.length
      });
      let refreshFailed = false;
      try {
        await onCompleted();
      } catch {
        refreshFailed = true;
      }
      setDone(true);
      setFeedback({
        tone: refreshFailed ? "warning" : "success",
        message: refreshFailed
          ? "原子提交已经成功，但页面刷新失败；请手动刷新确认最新 revision，不要重复提交。"
          : "渠道、密钥、deployment 和路由已在一次 CAS 提交中生效，无需重启。"
      });
    } catch (cause) {
      setValidated(false);
      setFeedback({
        tone: "error",
        message: `${errorMessage(cause)}；未收到成功确认。服务端不会留下半套配置，但超时或断线时整套提交可能已经生效；请先刷新配置确认，勿直接重试。`
      });
    } finally {
      setBusy("");
    }
  };

  const copyClientSettings = async () => {
    const settings = loadSettings();
    const clientBaseUrl = `${settings.apiBaseUrl.replace(/\/+$/, "")}/v1`;
    await copyText(`Base URL: ${clientBaseUrl}\nAPI Key: ${settings.apiKey}\n模型名: memory-auto`);
    setFeedback({ tone: "success", message: "客户端配置已复制；内容包含访问密钥，请妥善保管。" });
  };

  const embeddingReplacement =
    embeddingEnabled && embeddingRoute && embeddingRouteOperation === "replace";
  const privateNetworkList = allowedPrivateNetworks
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);

  return (
    <section className="panel provider-editor-section provider-wizard" aria-labelledby="new-channel-title">
      <div className="panel-header provider-section-header">
        <div>
          <h2 id="new-channel-title">新建渠道</h2>
          <p>先只读发现模型，再校验整套 bundle；只有最后确认时才原子保存。</p>
        </div>
        <button type="button" className="icon-button" onClick={onClose} aria-label="关闭新建渠道" title="关闭">
          <X size={16} aria-hidden />
        </button>
      </div>

      <div className="provider-wizard-body">
        {feedback && (
          <div className={`provider-feedback is-${feedback.tone}`} role={feedback.tone === "error" ? "alert" : "status"}>
            {feedback.tone === "success" ? <CheckCircle2 size={18} aria-hidden /> : <TriangleAlert size={18} aria-hidden />}
            <span>{feedback.message}</span>
          </div>
        )}

        {done ? (
          <div className="provider-wizard-step">
            <h3><CheckCircle2 size={18} aria-hidden /> 模型配置完成</h3>
            <p className="provider-wizard-hint">
              已保存 {appliedSummary.deployments} 个 deployment，变更 {appliedSummary.routes} 条路由。现在可连接 OpenAI 兼容客户端。
            </p>
            <div className="client-config-summary">
              <span><small>类型</small><strong>OpenAI 兼容 · Chat Completions</strong></span>
              <span><small>Base URL</small><code>{loadSettings().apiBaseUrl.replace(/\/+$/, "")}/v1</code></span>
              <span><small>模型名</small><code>memory-auto</code></span>
            </div>
            <div className="provider-wizard-actions">
              <button type="button" className="secondary-button" onClick={() => void copyClientSettings()}>
                <ClipboardCopy size={16} aria-hidden />复制客户端配置
              </button>
              <button type="button" className="primary-button" onClick={onClose}>完成</button>
            </div>
          </div>
        ) : (
          <>
            <div className="provider-wizard-step">
              <h3><span className="provider-step-index" aria-hidden>1</span>选择并检查渠道</h3>
              <div className="provider-preset-grid" role="radiogroup" aria-label="渠道预设">
                {CHANNEL_PRESETS.map((item) => (
                  <button
                    key={item.id}
                    type="button"
                    role="radio"
                    aria-checked={preset === item.id}
                    className={`provider-preset-card${preset === item.id ? " is-active" : ""}`}
                    onClick={() => selectPreset(item.id)}
                    disabled={Boolean(busy)}
                  >
                    <strong>{item.label}</strong><span>{item.base_url}</span>
                  </button>
                ))}
                <button
                  type="button"
                  role="radio"
                  aria-checked={preset === "custom"}
                  className={`provider-preset-card${preset === "custom" ? " is-active" : ""}`}
                  onClick={() => selectPreset("custom")}
                  disabled={Boolean(busy)}
                >
                  <strong>自定义渠道</strong><span>填写官方 OpenAI 兼容地址</span>
                </button>
              </div>

              {preset === "custom" && (
                <div className="provider-field-grid">
                  <label className="field-block">
                    <span>渠道简称</span>
                    <input
                      value={operator}
                      onChange={(event) => { setOperator(event.target.value); invalidateDiscovery(); }}
                      spellCheck={false}
                      placeholder="例如 vendor-cn"
                      disabled={Boolean(busy)}
                    />
                  </label>
                  <label className="field-block">
                    <span>官方 API 地址（远程必须 HTTPS）</span>
                    <input
                      value={baseUrl}
                      onChange={(event) => { setBaseUrl(event.target.value); invalidateDiscovery(); }}
                      spellCheck={false}
                      placeholder="https://api.example.com/v1"
                      disabled={Boolean(busy)}
                    />
                  </label>
                </div>
              )}

              <label className="field-block">
                <span>API Key（仅组件内存；发现和校验均不会保存）</span>
                <div className="secret-field">
                  <input
                    type={showApiKey ? "text" : "password"}
                    value={apiKey}
                    onChange={(event) => { setApiKey(event.target.value); invalidateDiscovery(); }}
                    autoComplete="new-password"
                    spellCheck={false}
                    placeholder="sk-..."
                    disabled={Boolean(busy)}
                  />
                  <button type="button" className="icon-button" onClick={() => setShowApiKey((current) => !current)} aria-label={showApiKey ? "隐藏渠道密钥" : "显示渠道密钥"}>
                    {showApiKey ? <EyeOff size={16} aria-hidden /> : <Eye size={16} aria-hidden />}
                  </button>
                </div>
              </label>

              <details className="provider-inline-advanced">
                <summary>高级：计费、权限与兼容设置</summary>
                <div className="provider-field-grid">
                  <label className="field-block">
                    <span>适配器</span>
                    <select value={adapter} onChange={(event) => { setAdapter(event.target.value as ModelGatewayAdapter); invalidateDiscovery(); }} disabled={Boolean(busy)}>
                      {ADAPTER_OPTIONS.map((option) => <option key={option} value={option}>{option}</option>)}
                    </select>
                  </label>
                  <label className="field-block">
                    <span>计费计划</span>
                    <select value={plan} onChange={(event) => { setPlan(event.target.value as ModelGatewayPlan); invalidateBundle(); }} disabled={Boolean(busy)}>
                      {PLAN_OPTIONS.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
                    </select>
                  </label>
                  <label className="field-block">
                    <span>使用范围</span>
                    <select value={usageScope} onChange={(event) => { setUsageScope(event.target.value as ModelGatewayUsageScope); invalidateBundle(); }} disabled={Boolean(busy)}>
                      <option value="backend_allowed">允许记忆后台任务</option>
                      <option value="interactive_only">仅人工交互</option>
                      <option value="disabled">保存但禁用</option>
                    </select>
                  </label>
                  <label className="field-block">
                    <span>鉴权 Header</span>
                    <select value={authType} onChange={(event) => { setAuthType(event.target.value as "bearer" | "x-api-key"); invalidateDiscovery(); }} disabled={Boolean(busy)}>
                      <option value="bearer">Authorization: Bearer</option>
                      <option value="x-api-key">X-API-Key</option>
                    </select>
                  </label>
                  <label className="field-block">
                    <span>允许的私网 CIDR（逗号分隔）</span>
                    <input
                      value={allowedPrivateNetworks}
                      onChange={(event) => { setAllowedPrivateNetworks(event.target.value); invalidateDiscovery(); }}
                      spellCheck={false}
                      placeholder="例如 192.168.50.0/24"
                      disabled={Boolean(busy)}
                    />
                  </label>
                </div>
                {privateNetworkList.length > 0 && <p className="provider-wizard-hint">只会允许显式列出的私网段，不会放开任意内网访问。</p>}
              </details>

              {usageScope !== "backend_allowed" && (
                <div className="provider-feedback is-warning" role="status">
                  <TriangleAlert size={18} aria-hidden />
                  <span>此渠道不会接入 memory.* / knowledge.* 后台路由；现有路由将保持不变。</span>
                </div>
              )}

              {discoveryCheck && (
                <div className={`provider-check-result is-${discoveryCheck.level}`} role="status">
                  {discoveryCheck.level === "ok" ? <CheckCircle2 size={15} aria-hidden /> : <TriangleAlert size={15} aria-hidden />}
                  <span>{discoveryCheck.detail}</span>
                </div>
              )}
              <div className="provider-wizard-actions">
                <button
                  type="button"
                  className="primary-button"
                  onClick={() => void discover()}
                  disabled={!hasAdminKey || Boolean(busy) || !operator.trim() || !baseUrl.trim() || !apiKey.trim()}
                >
                  <PlugZap size={16} aria-hidden />
                  {busy === "discover" ? "正在只读发现" : discovery ? "重新发现模型" : "只读发现模型"}
                </button>
              </div>
            </div>

            {discovery && (
              <>
                <div className="provider-wizard-step">
                  <h3><span className="provider-step-index" aria-hidden>2</span>选择模型与路由</h3>
                  <div className="provider-field-grid">
                    <label className="field-block">
                      <span>聊天模型</span>
                      {models.length > 0 ? (
                        <select value={chatModel} onChange={(event) => { setChatModel(event.target.value); invalidateBundle(); }} disabled={Boolean(busy)}>
                          <option value="">请选择一个模型</option>
                          {models.map((model) => (
                            <option key={model.id} value={model.id}>
                              {model.id}{model.aliases.length ? `（别名：${model.aliases.join("、")}）` : ""}
                            </option>
                          ))}
                        </select>
                      ) : (
                        <input value={chatModel} onChange={(event) => { setChatModel(event.target.value); invalidateBundle(); }} spellCheck={false} placeholder="精确 upstream_model ID" disabled={Boolean(busy)} />
                      )}
                    </label>
                    <label className="field-block">
                      <span>现有文本路由</span>
                      <select
                        value={chatRouteOperation}
                        onChange={(event) => { setChatRouteOperation(event.target.value as ModelGatewayRouteOperation); invalidateBundle(); }}
                        disabled={!canAssignBackendRoutes || Boolean(busy)}
                      >
                        <option value="keep">保持不变（默认）</option>
                        <option value="prepend">设为首选并保留现有目标</option>
                        <option value="append">追加到现有目标末尾</option>
                        <option value="replace">替换为这个模型</option>
                      </select>
                    </label>
                  </div>
                  {canAssignBackendRoutes && missingChatRoutes.length > 0 && (
                    <p className="provider-wizard-hint">
                      缺失的 {missingChatRoutes.length} 条文本用途路由会自动创建；已经存在的路由默认 keep，不会被覆盖。
                    </p>
                  )}

                  <details className="provider-inline-advanced">
                    <summary>高级：模型能力与适配 profile</summary>
                    <div className="provider-field-grid">
                      <label className="field-block">
                        <span>模型作者</span>
                        <input value={modelAuthor} onChange={(event) => { setModelAuthor(event.target.value); invalidateBundle(); }} spellCheck={false} placeholder="留空则记为 unknown" disabled={Boolean(busy)} />
                      </label>
                      <label className="field-block">
                        <span>适配 profile</span>
                        <select value={adapterProfile} onChange={(event) => { setAdapterProfile(event.target.value as "inherit" | "dashscope_deepseek_v4"); invalidateBundle(); }} disabled={Boolean(busy)}>
                          <option value="inherit">继承渠道适配器</option>
                          <option value="dashscope_deepseek_v4">DashScope DeepSeek V4 Flash/Pro</option>
                        </select>
                      </label>
                      <label className="field-block">
                        <span>默认推理模式</span>
                        <select value={reasoningDefault} onChange={(event) => { setReasoningDefault(event.target.value as "inherit" | "enabled" | "disabled"); invalidateBundle(); }} disabled={Boolean(busy)}>
                          <option value="inherit">继承请求</option>
                          <option value="enabled">默认开启</option>
                          <option value="disabled">默认关闭</option>
                        </select>
                      </label>
                    </div>
                    <fieldset className="provider-capability-field" disabled={Boolean(busy)}>
                      <legend>只有服务商明确支持时才勾选</legend>
                      <div className="provider-capability-grid">
                        {CAPABILITY_OPTIONS.map((option) => (
                          <label key={option.key}>
                            <input type="checkbox" checked={Boolean(capabilities[option.key])} onChange={(event) => toggleCapability(option.key, event.target.checked)} />
                            <span>{option.label}</span>
                          </label>
                        ))}
                      </div>
                    </fieldset>
                  </details>

                  <label className="provider-route-toggle provider-embedding-toggle">
                    <input type="checkbox" checked={embeddingEnabled} onChange={(event) => { setEmbeddingEnabled(event.target.checked); invalidateBundle(); }} disabled={Boolean(busy)} />
                    <span>同时保存一个向量模型（可选）</span>
                  </label>

                  {embeddingEnabled && (
                    <div className="provider-field-grid">
                      <label className="field-block">
                        <span>向量模型</span>
                        <input value={embeddingModel} onChange={(event) => updateEmbeddingModel(event.target.value)} list="new-channel-models" spellCheck={false} placeholder="精确 embedding 模型 ID" disabled={Boolean(busy)} />
                      </label>
                      <label className="field-block">
                        <span>向量维度</span>
                        <input value={embeddingDimensions} onChange={(event) => updateEmbeddingDimensions(event.target.value)} inputMode="numeric" spellCheck={false} placeholder="例如 1024" disabled={Boolean(busy)} />
                      </label>
                      <label className="field-block">
                        <span>向量空间名称</span>
                        <input value={embeddingSpace} onChange={(event) => { setEmbeddingSpace(event.target.value); setEmbeddingSpaceEdited(true); invalidateBundle(); }} spellCheck={false} placeholder="不可与不同模型/维度混用" disabled={Boolean(busy)} />
                      </label>
                      <label className="field-block">
                        <span>现有向量路由</span>
                        <select
                          value={embeddingRouteOperation}
                          onChange={(event) => { setEmbeddingRouteOperation(event.target.value as ModelGatewayRouteOperation); invalidateBundle(); }}
                          disabled={!canAssignBackendRoutes || !embeddingRoute || Boolean(busy)}
                        >
                          <option value="keep">保持不变（默认）</option>
                          <option value="replace">切换到新向量空间</option>
                        </select>
                      </label>
                    </div>
                  )}
                  <datalist id="new-channel-models">
                    {models.map((model) => <option key={model.id} value={model.id} />)}
                  </datalist>

                  {embeddingReplacement && (
                    <div className="provider-feedback is-warning" role="alert">
                      <TriangleAlert size={18} aria-hidden />
                      <span>
                        替换向量路由会从 {currentEmbedding?.embedding_space || "未知旧空间"} / {currentEmbedding?.dimensions || "未知"} 维
                        切换到 {embeddingSpace || "待填写新空间"} / {embeddingDimensions || "待填写"} 维。已有记忆和知识向量必须完整重索引；旧空间向量不会与新空间混用。
                      </span>
                    </div>
                  )}
                </div>

                <div className="provider-wizard-step">
                  <h3><span className="provider-step-index" aria-hidden>3</span>校验并原子应用</h3>
                  <p className="provider-wizard-hint">
                    “校验完整配置”仍为零落盘；校验通过后才可执行一次 CAS 原子提交。revision 冲突会安全失败并要求刷新。
                  </p>
                  <div className="provider-wizard-actions">
                    <button type="button" className="secondary-button" onClick={() => void validate()} disabled={!chatModel.trim() || Boolean(busy)}>
                      <ShieldCheck size={16} aria-hidden />
                      {busy === "validate" ? "正在校验" : validated ? "重新校验" : "校验完整配置"}
                    </button>
                    <button type="button" className="primary-button" onClick={() => void apply()} disabled={!validated || Boolean(busy)}>
                      <Save size={16} aria-hidden />
                      {busy === "apply" ? "正在原子应用" : "确认并原子应用"}
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
