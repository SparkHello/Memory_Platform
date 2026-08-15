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
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { MemoryApi } from "../../api";
import type { ConfirmFn } from "../../hooks/useConfirm";
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
import { channelUrlKey, distinctEmbeddingBaseUrl } from "../../utils/channelUrl";
import { filterDiscoveredChatModels } from "../../utils/discoveredModels";
import { errorMessage } from "../../utils/format";
import { CAPABILITY_OPTIONS, CHAT_ROUTE_IDS, type ProviderFeedback } from "./providerShared";

const EMBEDDING_ROUTE_ID = "memory.embedding";

const CHANNEL_PRESETS = [
  {
    id: "deepseek",
    label: "DeepSeek 官方",
    channel_operator: "deepseek",
    base_url: "https://api.deepseek.com",
    adapter: "deepseek",
    plan: "payg",
    usage_scope: "backend_allowed",
    auth_type: "bearer" as const
  },
  {
    id: "claude",
    label: "Anthropic Claude",
    channel_operator: "anthropic",
    // OpenAI-compatible base URL if you use a relay; official Messages API is not this path.
    base_url: "https://api.anthropic.com/v1",
    adapter: "generic",
    plan: "payg",
    usage_scope: "backend_allowed",
    auth_type: "x-api-key" as const
  },
  {
    id: "gemini",
    label: "Google Gemini（OpenAI 兼容）",
    channel_operator: "google",
    base_url: "https://generativelanguage.googleapis.com/v1beta/openai/",
    adapter: "generic",
    plan: "payg",
    usage_scope: "backend_allowed",
    auth_type: "bearer" as const
  },
  {
    id: "dashscope-cn",
    label: "阿里云百炼按量",
    channel_operator: "dashscope",
    base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1",
    adapter: "dashscope_openai",
    plan: "payg",
    usage_scope: "backend_allowed",
    auth_type: "bearer" as const
  }
] as const satisfies ReadonlyArray<{
  id: string;
  label: string;
  channel_operator: string;
  base_url: string;
  adapter: ModelGatewayAdapter;
  plan: ModelGatewayPlan;
  usage_scope: ModelGatewayUsageScope;
  auth_type: "bearer" | "x-api-key";
}>;

type PresetId = (typeof CHANNEL_PRESETS)[number]["id"] | "custom";

/** Empty fields when user picks 自定义 — do not inherit the previous preset. */
const CUSTOM_CHANNEL_DEFAULTS = {
  channel_operator: "",
  base_url: "",
  adapter: "generic" as ModelGatewayAdapter,
  plan: "payg" as ModelGatewayPlan,
  usage_scope: "backend_allowed" as ModelGatewayUsageScope,
  auth_type: "bearer" as const
};

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
  { value: "token_plan", label: "Token Plan（自管）" },
  { value: "coding_plan", label: "Coding Plan（自管）" },
  { value: "direct_tool_only", label: "仅官方工具直连" },
  { value: "custom", label: "自定义" }
];

/** Clash/Surge TUN fake-ip range; only applied when the user opts in. */
const FAKE_IP_CIDR = "198.18.0.0/15";

function embeddingHostSlug(embeddingUrl: string, chatUrl: string): string {
  const embed = channelUrlKey(embeddingUrl);
  const chat = channelUrlKey(chatUrl);
  if (!embed || !chat || embed === chat) return "";
  try {
    return new URL(embed).hostname.toLowerCase();
  } catch {
    return "";
  }
}

function suggestEmbeddingSpace(
  operator: string,
  model: string,
  dimensions: string,
  embeddingUrl = "",
  chatUrl = ""
): string {
  const slug = model
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9._:-]+/g, "-")
    .replace(/^[-._:]+|[-._:]+$/g, "");
  if (!slug) return "";
  const owner = operator.trim().toLowerCase() || "channel";
  const host = embeddingHostSlug(embeddingUrl, chatUrl);
  const prefix = host ? `${owner}.${host}` : owner;
  const dims = dimensions.trim();
  return `${prefix}.${slug}${dims ? `:${dims}` : ""}`;
}

function parseCidrList(value: string): string[] {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function joinCidrList(items: string[]): string {
  return Array.from(new Set(items)).join(", ");
}

function cidrListIncludesFakeIp(items: string[]): boolean {
  return items.some((item) => item === FAKE_IP_CIDR || item.startsWith("198.18."));
}

export function looksLikeFakeIpDetail(detail: string): boolean {
  return /198\.18|fake-ip/i.test(detail);
}

function FakeIpOptIn({
  checked,
  disabled,
  onChange
}: {
  checked: boolean;
  disabled: boolean;
  onChange: (enabled: boolean) => void;
}) {
  return (
    <label className="inline-check">
      <input
        type="checkbox"
        checked={checked}
        onChange={(event) => onChange(event.target.checked)}
        disabled={disabled}
      />
      使用 Clash/Surge 等 TUN fake-ip（写入 198.18.0.0/15）
    </label>
  );
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
  confirm,
  onClose,
  onCompleted
}: {
  api: MemoryApi;
  adminKey: string;
  control: ModelGatewayControlSnapshot;
  confirm: ConfirmFn;
  onClose: () => void;
  onCompleted: () => Promise<void> | void;
}) {
  const [preset, setPreset] = useState<PresetId | "">("");
  const [operator, setOperator] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [adapter, setAdapter] = useState<ModelGatewayAdapter>(CUSTOM_CHANNEL_DEFAULTS.adapter);
  const [plan, setPlan] = useState<ModelGatewayPlan>(CUSTOM_CHANNEL_DEFAULTS.plan);
  const [usageScope, setUsageScope] = useState<ModelGatewayUsageScope>(CUSTOM_CHANNEL_DEFAULTS.usage_scope);
  const [authType, setAuthType] = useState<"bearer" | "x-api-key">(CUSTOM_CHANNEL_DEFAULTS.auth_type);
  const [allowedPrivateNetworks, setAllowedPrivateNetworks] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [showApiKey, setShowApiKey] = useState(false);
  const [discovery, setDiscovery] = useState<ModelGatewayChannelDiscoverResult | null>(null);
  const [chatModel, setChatModel] = useState("");
  const [chatModelQuery, setChatModelQuery] = useState("");
  const [showFakeIpOptIn, setShowFakeIpOptIn] = useState(false);
  const [modelAuthor, setModelAuthor] = useState("");
  const [adapterProfile, setAdapterProfile] = useState<"inherit" | "dashscope_deepseek_v4">("inherit");
  const [reasoningDefault, setReasoningDefault] = useState<"inherit" | "enabled" | "disabled">("inherit");
  const [capabilities, setCapabilities] = useState<ModelGatewayCapabilities>({});
  const [chatRouteOperation, setChatRouteOperation] = useState<ModelGatewayRouteOperation>("keep");
  const [embeddingEnabled, setEmbeddingEnabled] = useState(false);
  const [embeddingModel, setEmbeddingModel] = useState("");
  const [embeddingDimensions, setEmbeddingDimensions] = useState("");
  const [embeddingBaseUrl, setEmbeddingBaseUrl] = useState("");
  const [embeddingBaseUrlEdited, setEmbeddingBaseUrlEdited] = useState(false);
  const [embeddingSpace, setEmbeddingSpace] = useState("");
  const [embeddingSpaceEdited, setEmbeddingSpaceEdited] = useState(false);
  const [embeddingRouteOperation, setEmbeddingRouteOperation] = useState<ModelGatewayRouteOperation>("keep");
  const [validatedSignature, setValidatedSignature] = useState<string | null>(null);
  const [discoverySignature, setDiscoverySignature] = useState<string | null>(null);
  const [busy, setBusy] = useState<"" | "discover" | "validate" | "apply" | "probe">("");
  const [probeNote, setProbeNote] = useState<string | null>(null);
  const [feedback, setFeedbackState] = useState<ProviderFeedback | null>(null);
  const [done, setDone] = useState(false);
  const [appliedSummary, setAppliedSummary] = useState({ deployments: 0, routes: 0 });
  /** One-time chat token minted after successful apply; never Console key. */
  const [clientChatToken, setClientChatToken] = useState<string | null>(null);
  const [clientTokenError, setClientTokenError] = useState<string | null>(null);
  const [clientTokenReused, setClientTokenReused] = useState(false);
  const [reembedNote, setReembedNote] = useState<string | null>(null);
  const [showClientToken, setShowClientToken] = useState(false);
  const [clientTokenCopied, setClientTokenCopied] = useState(false);

  // discovery 的有效性由输入签名派生：签名与发起发现时不同即视为过期，
  // 等价于原来在每个字段 onChange 里手动 setDiscovery(null)。
  const discoveryInputsSignature = JSON.stringify({
    channel_operator: operator.trim(),
    base_url: baseUrl.trim(),
    adapter,
    auth_type: authType,
    allowed_private_networks: parseCidrList(allowedPrivateNetworks),
    candidate_key: apiKey.trim()
  });
  const discoveryStale = discovery !== null && discoverySignature !== discoveryInputsSignature;
  const effectiveDiscovery = discoveryStale ? null : discovery;

  const hasAdminKey = Boolean(adminKey.trim());
  const models = effectiveDiscovery?.models || [];
  const visibleChatModels = useMemo(
    () => filterDiscoveredChatModels(models, chatModelQuery),
    [models, chatModelQuery]
  );
  const discoveryCheck = effectiveDiscovery?.report.connections[0];
  const canAssignBackendRoutes = usageScope === "backend_allowed";
  const embeddingRoute = control.routes.find((route) => route.id === EMBEDDING_ROUTE_ID);
  const currentEmbedding = embeddingRoute?.targets[0]
    ? control.deployments.find((deployment) => deployment.id === embeddingRoute.targets[0])
    : undefined;
  const privateNetworkList = useMemo(
    () => parseCidrList(allowedPrivateNetworks),
    [allowedPrivateNetworks]
  );
  const fakeIpEnabled = cidrListIncludesFakeIp(privateNetworkList);
  const clientBaseUrl = `${loadSettings().apiBaseUrl.replace(/\/+$/, "")}/v1`;

  useEffect(() => {
    if (!embeddingBaseUrlEdited) setEmbeddingBaseUrl(baseUrl);
  }, [baseUrl, embeddingBaseUrlEdited]);

  const missingChatRoutes = useMemo(
    () => CHAT_ROUTE_IDS.filter((id) => !control.routes.some((route) => route.id === id)),
    [control.routes]
  );

  // 反馈只经由带序号的 setter 写入；字段改动（签名变化）且期间没有流程写过反馈时，
  // 由 buildBundle 下方的签名 effect 统一清掉过期提示，代替过去每个 onChange 里的手动 invalidate。
  const feedbackSeqRef = useRef(0);
  const setFeedback = useCallback((value: ProviderFeedback | null) => {
    feedbackSeqRef.current += 1;
    setFeedbackState(value);
  }, []);

  const selectPreset = (id: PresetId) => {
    setPreset(id);
    if (id === "custom") {
      // Always blank generic form — never leave a previous preset's URL/operator.
      setOperator(CUSTOM_CHANNEL_DEFAULTS.channel_operator);
      setBaseUrl(CUSTOM_CHANNEL_DEFAULTS.base_url);
      setAdapter(CUSTOM_CHANNEL_DEFAULTS.adapter);
      setPlan(CUSTOM_CHANNEL_DEFAULTS.plan);
      setUsageScope(CUSTOM_CHANNEL_DEFAULTS.usage_scope);
      setAuthType(CUSTOM_CHANNEL_DEFAULTS.auth_type);
    } else {
      const found = CHANNEL_PRESETS.find((item) => item.id === id);
      if (found) {
        setOperator(found.channel_operator);
        setBaseUrl(found.base_url);
        setAdapter(found.adapter);
        setPlan(found.plan);
        setUsageScope(found.usage_scope);
        setAuthType(found.auth_type);
      }
    }
  };

  const toggleCapability = (key: keyof ModelGatewayCapabilities, checked: boolean) => {
    setCapabilities((current) => {
      const next = { ...current, [key]: checked };
      if (key === "parallel_tools" && checked) next.tools = true;
      if (key === "tools" && !checked) next.parallel_tools = false;
      return next;
    });
  };

  const refreshSuggestedSpace = (
    model = embeddingModel,
    dimensions = embeddingDimensions,
    embedUrl = embeddingBaseUrl
  ) => {
    if (embeddingSpaceEdited) return;
    setEmbeddingSpace(suggestEmbeddingSpace(operator, model, dimensions, embedUrl, baseUrl));
  };

  const updateEmbeddingModel = (value: string) => {
    setEmbeddingModel(value);
    refreshSuggestedSpace(value);
  };

  const updateEmbeddingDimensions = (value: string) => {
    setEmbeddingDimensions(value);
    refreshSuggestedSpace(embeddingModel, value);
  };

  const updateEmbeddingBaseUrl = (value: string) => {
    setEmbeddingBaseUrl(value);
    setEmbeddingBaseUrlEdited(true);
    refreshSuggestedSpace(embeddingModel, embeddingDimensions, value);
  };

  const keepFeedbackRef = useRef(false);
  const setFakeIpEnabled = (enabled: boolean) => {
    const current = parseCidrList(allowedPrivateNetworks).filter(
      (item) => item !== FAKE_IP_CIDR
    );
    // 发现失败的错误文案本身在引导用户勾选此项后重试，所以这次失效保留该提示。
    keepFeedbackRef.current = true;
    if (enabled) {
      setAllowedPrivateNetworks(joinCidrList([...current, FAKE_IP_CIDR]));
    } else {
      setAllowedPrivateNetworks(joinCidrList(current));
    }
  };

  const discover = async () => {
    if (!hasAdminKey || busy || !operator.trim() || !baseUrl.trim() || !apiKey.trim()) return;
    setBusy("discover");
    setFeedback(null);
    try {
      const result = await api.discoverProviderChannel(
        {
          revision: control.revision,
          candidate_key: apiKey.trim(),
          channel_operator: operator.trim(),
          base_url: baseUrl.trim(),
          adapter,
          auth_type: authType,
          allowed_private_networks: parseCidrList(allowedPrivateNetworks),
          models_endpoint: "/models"
        },
        adminKey.trim()
      );
      if (result.persisted !== false) {
        throw new Error("模型发现响应没有确认零落盘，已停止后续配置");
      }
      setDiscovery(result);
      setDiscoverySignature(discoveryInputsSignature);
      setShowFakeIpOptIn(false);
      setChatModelQuery("");
      const chatIds = filterDiscoveredChatModels(result.models).map((model) => model.id);
      if (chatIds.length === 1) setChatModel(chatIds[0]);
      setFeedback({
        tone: result.models.length ? "success" : "warning",
        message: result.models.length
          ? `只读检查通过，发现 ${result.models.length} 个模型；候选渠道和密钥尚未保存。`
          : "只读检查通过，但没有返回模型列表；可以手动填写精确模型 ID。候选配置尚未保存。"
      });
    } catch (cause) {
      setDiscovery(null);
      setDiscoverySignature(null);
      const detail = errorMessage(cause, { credential: "admin" });
      // 只有明确命中 fake-ip 网段特征时才引导用户勾选 TUN 选项；
      // 泛化的"私网/安全校验"字样也会出现在与代理无关的错误里。
      const looksLikeFakeIp = looksLikeFakeIpDetail(detail) && !fakeIpEnabled;
      if (looksLikeFakeIp) setShowFakeIpOptIn(true);
      const looksLikeAuth =
        /401|403|鉴权|api.?key|密钥无效|unauthorized|invalid.?api/i.test(detail);
      setFeedback({
        tone: "error",
        message: looksLikeFakeIp
          ? `${detail} 密钥和渠道都还没保存。本机解析到了 TUN fake-ip：请勾选下方「使用 Clash/Surge 等 TUN fake-ip」后重试「只读发现模型」（与密钥是否正确无关）。`
          : looksLikeAuth
            ? `${detail} 密钥和渠道都还没保存。请核对 API Key 与 Base URL 是否属于同一渠道。`
            : `${detail} 密钥和渠道都还没保存（这是只读检查失败，不是密钥已写入）。`
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
    const embeddingUrl = distinctEmbeddingBaseUrl(baseUrl, embeddingBaseUrl);
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
        allowed_private_networks: parseCidrList(allowedPrivateNetworks)
      },
      ...(embeddingEnabled && embeddingUrl ? { embedding_base_url: embeddingUrl } : {}),
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

  // 校验有效性由 bundle 签名派生：任何字段改动都会让已校验签名失配，
  // 必须重新 dry_run——等价于原来散布在每个 setter/onChange 里的 invalidateBundle()。
  const bundle = buildBundle();
  const bundleSignature = JSON.stringify(bundle);
  const validated = bundle !== null && validatedSignature === bundleSignature;

  const wizardSigsRef = useRef<{ sigs: string; feedbackSeq: number } | null>(null);
  useEffect(() => {
    const sigs = `${discoveryInputsSignature}\n${bundleSignature}`;
    const prev = wizardSigsRef.current;
    wizardSigsRef.current = { sigs, feedbackSeq: feedbackSeqRef.current };
    // 签名没变、或本次签名变化由 discover/probe/apply 等流程自身引起（同批写过反馈）时保留提示。
    if (!prev || prev.sigs === sigs || prev.feedbackSeq !== feedbackSeqRef.current) return;
    if (keepFeedbackRef.current) {
      keepFeedbackRef.current = false;
      return;
    }
    setFeedbackState(null);
  }, [discoveryInputsSignature, bundleSignature]);

  // 发现输入变化后，基于旧发现结果的模型选择一并作废（等价原 invalidateDiscovery 清空）。
  // 派生清空不是用户编辑：标记一次，避免连锁清掉同批流程刚写下的提示（如 apply 成功）。
  useEffect(() => {
    if (discoveryStale && chatModel) {
      keepFeedbackRef.current = true;
      setChatModel("");
    }
  }, [discoveryStale, chatModel]);

  const probeCapabilities = async () => {
    if (!hasAdminKey || busy || !operator.trim() || !baseUrl.trim() || !apiKey.trim() || !chatModel.trim()) {
      setFeedback({
        tone: "error",
        message: "请先完成渠道发现并填写聊天模型 ID，再探测能力。"
      });
      return;
    }
    setBusy("probe");
    setFeedback(null);
    setProbeNote(null);
    try {
      const result = await api.probeProviderChannelCapabilities(
        {
          revision: control.revision,
          candidate_key: apiKey.trim(),
          channel_operator: operator.trim(),
          base_url: baseUrl.trim(),
          adapter,
          auth_type: authType,
          allowed_private_networks: parseCidrList(allowedPrivateNetworks),
          upstream_model: chatModel.trim(),
          probes: ["chat", "streaming", "tools", "reasoning", "json_object"]
        },
        adminKey.trim()
      );
      if (result.persisted !== false) {
        throw new Error("能力探测响应没有确认零落盘，已停止");
      }
      const next: ModelGatewayCapabilities = {
        tools: Boolean(result.capabilities.tools),
        parallel_tools: Boolean(result.capabilities.parallel_tools),
        reasoning: Boolean(result.capabilities.reasoning),
        multimodal_input: Boolean(result.capabilities.multimodal_input),
        json_object: Boolean(result.capabilities.json_object),
        json_schema: Boolean(result.capabilities.json_schema)
      };
      setCapabilities(next);
      const summary = CAPABILITY_OPTIONS.filter((option) => next[option.key])
        .map((option) => option.label)
        .join("、");
      const chatOk = result.details.chat?.ok;
      setProbeNote(result.note || null);
      setFeedback({
        tone: chatOk ? "success" : "error",
        message: chatOk
          ? `能力探测完成（会消耗少量额度，未保存密钥）。已勾选：${summary || "仅基础聊天/流式"}。可按需手工改勾选后再校验保存。`
          : `基础聊天探测失败：${result.details.chat?.detail || "未知错误"}。能力未改写。`
      });
    } catch (cause) {
      setFeedback({
        tone: "error",
        message: `${errorMessage(cause, { credential: "admin" })}；能力勾选未改，密钥未保存。`
      });
    } finally {
      setBusy("");
    }
  };

  const validate = async () => {
    if (!effectiveDiscovery || busy) return;
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
      setValidatedSignature(bundleSignature);
      const splitEmbedding =
        Boolean(result.embedding_connection_id) &&
        result.embedding_connection_id !== result.connection_id;
      setFeedback({
        tone: "success",
        message: splitEmbedding
          ? `配置检查通过：将保存 ${result.deployment_ids.length} 个模型；向量模型会走单独接入点并复用同一密钥。尚未写入。`
          : `配置检查通过：将保存 ${result.deployment_ids.length} 个模型，变更 ${result.changed_routes.length} 条用途。尚未写入。`
      });
    } catch (cause) {
      setValidatedSignature(null);
      setFeedback({
        tone: "error",
        message: `${errorMessage(cause, { credential: "admin" })}；完整 bundle 未落盘，现有配置保持不变。`
      });
    } finally {
      setBusy("");
    }
  };

  const apply = async () => {
    if (!validated || busy) return;
    if (!bundle) {
      setValidatedSignature(null);
      return;
    }
    setBusy("apply");
    setFeedback(null);
    setClientChatToken(null);
    setClientTokenError(null);
    setClientTokenReused(false);
    setReembedNote(null);
    setShowClientToken(false);
    setClientTokenCopied(false);
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

      let tokenOk = false;
      let reusedExisting = false;
      try {
        const tokens = await api.authTokens();
        reusedExisting = tokens.data.some((token) => token.role === "chat" && !token.revoked_at);
      } catch {
        reusedExisting = false;
      }
      if (reusedExisting) {
        setClientTokenReused(true);
        tokenOk = true;
      } else {
        try {
          const dateTag = new Date().toISOString().slice(0, 10);
          const tokenName = `${operator.trim() || "渠道"}-客户端-${dateTag}`.slice(0, 100);
          const created = await api.createAuthToken(tokenName, "chat");
          setClientChatToken(created.token);
          tokenOk = true;
        } catch (tokenCause) {
          setClientTokenError(errorMessage(tokenCause));
        }
      }

      if (embeddingEnabled) {
        try {
          const reembed = await api.reEmbedMemories({ scan: true });
          setReembedNote(
            reembed.re_embedded > 0
              ? `已为 ${reembed.re_embedded} 条缺少当前空间向量的记忆补齐向量。`
              : "当前没有需要补齐的记忆向量。"
          );
        } catch (reembedCause) {
          setReembedNote(
            `渠道已保存，但自动补齐记忆向量失败：${errorMessage(reembedCause)}。请到「记忆库」手动点「补齐向量」。`
          );
        }
      }

      setDone(true);
      if (refreshFailed) {
        setFeedback({
          tone: "warning",
          message:
            "配置已经保存，但页面刷新失败；请手动刷新确认最新状态，不要重复提交。"
        });
      } else if (!tokenOk) {
        setFeedback({
          tone: "warning",
          message:
            "渠道配置已生效，但自动创建 chat token 失败。请打开「接入信息」手动创建设备 token，切勿把 Console 密钥填进聊天客户端。"
        });
      } else if (reusedExisting) {
        setFeedback({
          tone: "success",
          message:
            "渠道已生效。已有可用的 chat token，请到「接入信息」使用现有客户端配置，不要把 Console 或 admin 密钥填进聊天应用。"
        });
      } else {
        setFeedback({
          tone: "success",
          message:
            "渠道已生效，并已创建仅用于聊天的 chat token（明文只显示一次）。请复制下方客户端配置，不要使用 Console 或 admin 密钥。" +
            "若之前为该渠道创建过 token，可在「接入信息」页撤销不再使用的旧 token。"
        });
      }
    } catch (cause) {
      setValidatedSignature(null);
      setFeedback({
        tone: "error",
        message: `${errorMessage(cause, { credential: "admin" })}；未收到成功确认。服务端不会留下半套配置，但超时或断线时整套提交可能已经生效；请先刷新配置确认，勿直接重试。`
      });
    } finally {
      setBusy("");
    }
  };

  const copyClientSettings = async () => {
    if (!clientChatToken) {
      setFeedback({
        tone: "error",
        message:
          "还没有 chat token 可复制。请到「接入信息」为该设备创建 chat token；不会用 Console 密钥冒充客户端密钥。"
      });
      return;
    }
    try {
      await copyText(
        `Base URL: ${clientBaseUrl}\nAPI Key: ${clientChatToken}\n模型名: memory-auto`
      );
      setClientTokenCopied(true);
      setFeedback({
        tone: "success",
        message: "客户端配置已复制（含 chat token）；请勿分享给他人或填入管理端。"
      });
    } catch (cause) {
      setFeedback({
        tone: "error",
        message:
          `复制失败：${errorMessage(cause)}。局域网 HTTP 页面浏览器可能禁止自动复制；` +
          "请点击「显示 token」后手动选中并复制。"
      });
    }
  };

  const requestClose = async () => {
    // 原子提交进行中禁止关闭：结果未知时关闭会让用户既拿不到 token 也不知道是否已生效。
    if (busy === "apply") return;
    if (done) {
      if (clientChatToken && !clientTokenCopied) {
        const confirmed = await confirm({
          title: "chat token 只显示这一次",
          message:
            "关闭后将无法再查看这个 chat token 明文，只能撤销后重新创建。确认已把客户端配置保存到安全的地方了吗？",
          confirmLabel: "已保存，关闭",
          cancelLabel: "返回复制",
          tone: "warning"
        });
        if (!confirmed) return;
      }
      onClose();
      return;
    }
    if (apiKey.trim()) {
      const confirmed = await confirm({
        title: "丢弃未保存的渠道配置？",
        message: "已填写的供应商 API Key 与表单内容不会保存，关闭后需要重新输入。",
        confirmLabel: "丢弃并关闭",
        cancelLabel: "继续编辑",
        tone: "warning"
      });
      if (!confirmed) return;
    }
    onClose();
  };

  const embeddingReplacement =
    embeddingEnabled && embeddingRoute && embeddingRouteOperation === "replace";

  return (
    <section className="panel provider-editor-section provider-wizard" aria-labelledby="new-channel-title">
      <div className="panel-header provider-section-header">
        <div>
          <h2 id="new-channel-title">新建渠道</h2>
          <p>先只读发现模型，再校验整套 bundle；只有最后确认时才原子保存。</p>
        </div>
        <button
          type="button"
          className="icon-button"
          onClick={() => void requestClose()}
          disabled={busy === "apply"}
          aria-label="关闭新建渠道"
          title={busy === "apply" ? "正在保存，暂不能关闭" : "关闭"}
        >
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
        {showFakeIpOptIn && !done && (
          <div className="provider-feedback is-warning" role="note">
            <label className="field-block provider-checkbox-field">
              <span>TUN fake-ip 代理</span>
              <FakeIpOptIn
                checked={fakeIpEnabled}
                disabled={Boolean(busy)}
                onChange={setFakeIpEnabled}
              />
            </label>
          </div>
        )}

        {done ? (
          <div className="provider-wizard-step">
            <h3><CheckCircle2 size={18} aria-hidden /> 模型配置完成</h3>
            <p className="provider-wizard-hint">
              已保存 {appliedSummary.deployments} 个模型，变更 {appliedSummary.routes} 条用途路由。
              {clientTokenReused
                ? "已有可用的 chat token，请到「接入信息」查看客户端三项填写。"
                : "请用下方 chat token 连接 OpenAI 兼容客户端。"}
              Console / admin 密钥不能填进 Chatbox 等聊天应用。
            </p>
            {reembedNote && <p className="provider-wizard-hint">{reembedNote}</p>}
            <div className="client-config-summary">
              <span><small>类型</small><strong>OpenAI 兼容 · Chat Completions</strong></span>
              <span><small>Base URL</small><code>{clientBaseUrl}</code></span>
              <span><small>模型名</small><code>memory-auto</code></span>
              <span>
                <small>API Key（chat token）</small>
                {clientChatToken ? (
                  <code className="client-token-value">
                    {showClientToken
                      ? clientChatToken
                      : `${clientChatToken.slice(0, 12)}…${clientChatToken.slice(-4)}`}
                  </code>
                ) : clientTokenReused ? (
                  <strong>请到「接入信息」使用已有 chat token</strong>
                ) : (
                  <strong className="text-warning">未创建 — 请到「接入信息」手动创建</strong>
                )}
              </span>
            </div>
            {clientTokenError && (
              <div className="provider-feedback is-warning" role="status">
                <TriangleAlert size={18} aria-hidden />
                <span>自动创建 chat token 失败：{clientTokenError}</span>
              </div>
            )}
            <div className="provider-wizard-actions">
              {clientChatToken && (
                <button
                  type="button"
                  className="secondary-button"
                  onClick={() => setShowClientToken((value) => !value)}
                >
                  {showClientToken ? <EyeOff size={16} aria-hidden /> : <Eye size={16} aria-hidden />}
                  {showClientToken ? "隐藏 token" : "显示 token"}
                </button>
              )}
              <button
                type="button"
                className="secondary-button"
                onClick={() => void copyClientSettings()}
                disabled={!clientChatToken}
              >
                <ClipboardCopy size={16} aria-hidden />复制客户端配置
              </button>
              <button type="button" className="primary-button" onClick={() => void requestClose()}>完成</button>
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
                  <strong>自定义渠道</strong><span>自行填写 OpenAI 兼容 Base URL（不会继承上一预设）</span>
                </button>
              </div>

              {preset === "custom" && (
                <div className="provider-field-grid">
                  <label className="field-block">
                    <span>渠道简称</span>
                    <input
                      value={operator}
                      onChange={(event) => { setOperator(event.target.value); }}
                      spellCheck={false}
                      placeholder="例如 my-proxy"
                      disabled={Boolean(busy)}
                    />
                  </label>
                  <label className="field-block">
                    <span>官方 API 地址（远程必须 HTTPS）</span>
                    <input
                      value={baseUrl}
                      onChange={(event) => { setBaseUrl(event.target.value); }}
                      spellCheck={false}
                      placeholder="https://api.example.com/v1"
                      disabled={Boolean(busy)}
                    />
                  </label>
                  <p className="muted" style={{ gridColumn: "1 / -1", margin: 0 }}>
                    套餐条款与可用模型由你的提供商决定；本网关不再按 Token/Coding Plan 限制后台记忆任务。
                  </p>
                </div>
              )}

              <label className="field-block">
                <span>API Key（仅组件内存；发现和校验均不会保存）</span>
                <div className="secret-field">
                  <input
                    type={showApiKey ? "text" : "password"}
                    value={apiKey}
                    onChange={(event) => { setApiKey(event.target.value); }}
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
                    <select value={adapter} onChange={(event) => { setAdapter(event.target.value as ModelGatewayAdapter); }} disabled={Boolean(busy)}>
                      {ADAPTER_OPTIONS.map((option) => <option key={option} value={option}>{option}</option>)}
                    </select>
                  </label>
                  <label className="field-block">
                    <span>计费计划</span>
                    <select value={plan} onChange={(event) => { setPlan(event.target.value as ModelGatewayPlan); }} disabled={Boolean(busy)}>
                      {PLAN_OPTIONS.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
                    </select>
                  </label>
                  <label className="field-block">
                    <span>使用范围</span>
                    <select value={usageScope} onChange={(event) => { setUsageScope(event.target.value as ModelGatewayUsageScope); }} disabled={Boolean(busy)}>
                      <option value="backend_allowed">允许记忆后台任务</option>
                      <option value="interactive_only">仅人工交互</option>
                      <option value="disabled">保存但禁用</option>
                    </select>
                  </label>
                  <label className="field-block">
                    <span>鉴权 Header</span>
                    <select value={authType} onChange={(event) => { setAuthType(event.target.value as "bearer" | "x-api-key"); }} disabled={Boolean(busy)}>
                      <option value="bearer">Authorization: Bearer</option>
                      <option value="x-api-key">X-API-Key</option>
                    </select>
                  </label>
                  <label className="field-block">
                    <span>允许的私网 CIDR（逗号分隔）</span>
                    <input
                      value={allowedPrivateNetworks}
                      onChange={(event) => { setAllowedPrivateNetworks(event.target.value); }}
                      spellCheck={false}
                      placeholder="例如 192.168.50.0/24 或 198.18.0.0/15"
                      disabled={Boolean(busy)}
                    />
                  </label>
                  <label className="field-block provider-checkbox-field">
                    <span>TUN fake-ip 代理</span>
                    <FakeIpOptIn
                      checked={fakeIpEnabled}
                      disabled={Boolean(busy)}
                      onChange={setFakeIpEnabled}
                    />
                  </label>
                </div>
                <p className="provider-wizard-hint">
                  只会允许显式列出的私网段，不会放开任意内网访问。
                  {fakeIpEnabled
                    ? " 已允许 RFC 2544 fake-ip 段；仅在你确实使用此类代理时勾选。"
                    : " 若发现模型时报「解析到未允许的私网/198.18」，再勾选 fake-ip。"}
                </p>
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
                  {busy === "discover" ? "正在只读发现" : effectiveDiscovery ? "重新发现模型" : "只读发现模型"}
                </button>
              </div>
            </div>

            {effectiveDiscovery && (
              <>
                <div className="provider-wizard-step">
                  <h3><span className="provider-step-index" aria-hidden>2</span>选择模型与路由</h3>
                  <div className="provider-field-grid">
                    <div className="field-block">
                      <span>聊天模型</span>
                      {models.length > 0 ? (
                        <>
                          {models.length > 8 && (
                            <input
                              value={chatModelQuery}
                              onChange={(event) => setChatModelQuery(event.target.value)}
                              spellCheck={false}
                              placeholder="输入以筛选模型 ID"
                              aria-label="过滤聊天模型"
                              disabled={Boolean(busy)}
                            />
                          )}
                          <select
                            value={chatModel}
                            onChange={(event) => { setChatModel(event.target.value); }}
                            disabled={Boolean(busy)}
                            aria-label="聊天模型"
                          >
                            <option value="">请选择一个模型</option>
                            {chatModel &&
                              !visibleChatModels.some((model) => model.id === chatModel) && (
                                <option value={chatModel}>{chatModel}</option>
                              )}
                            {visibleChatModels.map((model) => (
                              <option key={model.id} value={model.id}>
                                {model.id}{model.aliases.length ? `（别名：${model.aliases.join("、")}）` : ""}
                              </option>
                            ))}
                          </select>
                          {models.length !== visibleChatModels.length && !chatModelQuery.trim() && (
                            <p className="provider-wizard-hint">
                              已隐藏 {models.length - visibleChatModels.length} 个嵌入/语音/图像模型；向量模型请在下方单独填写。
                            </p>
                          )}
                        </>
                      ) : (
                        <input value={chatModel} onChange={(event) => { setChatModel(event.target.value); }} spellCheck={false} placeholder="精确 upstream_model ID" disabled={Boolean(busy)} aria-label="聊天模型" />
                      )}
                    </div>
                    <label className="field-block">
                      <span>现有文本路由</span>
                      <select
                        value={chatRouteOperation}
                        onChange={(event) => { setChatRouteOperation(event.target.value as ModelGatewayRouteOperation); }}
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
                    <summary>模型能力与适配（不填会影响路由）</summary>
                    <p className="provider-wizard-hint">
                      <strong>未勾选 = 路由视为不支持</strong>
                      ：例如没勾「工具调用」时，客户端带 tools 的请求不会用这个模型。
                      流式默认开启。可用下方探测自动勾选（会向供应商发几次极短请求，消耗少量额度，密钥仍不落盘）。
                    </p>
                    <div className="provider-field-grid">
                      <label className="field-block">
                        <span>模型作者</span>
                        <input value={modelAuthor} onChange={(event) => { setModelAuthor(event.target.value); }} spellCheck={false} placeholder="留空则记为 unknown" disabled={Boolean(busy)} />
                      </label>
                      <label className="field-block">
                        <span>适配 profile</span>
                        <select value={adapterProfile} onChange={(event) => { setAdapterProfile(event.target.value as "inherit" | "dashscope_deepseek_v4"); }} disabled={Boolean(busy)}>
                          <option value="inherit">继承渠道适配器</option>
                          <option value="dashscope_deepseek_v4">DashScope DeepSeek V4 Flash/Pro</option>
                        </select>
                      </label>
                      <label className="field-block">
                        <span>默认推理模式</span>
                        <select value={reasoningDefault} onChange={(event) => { setReasoningDefault(event.target.value as "inherit" | "enabled" | "disabled"); }} disabled={Boolean(busy)}>
                          <option value="inherit">继承请求</option>
                          <option value="enabled">默认开启</option>
                          <option value="disabled">默认关闭</option>
                        </select>
                      </label>
                    </div>
                    <fieldset className="provider-capability-field" disabled={Boolean(busy)}>
                      <legend>声明此模型支持的能力</legend>
                      <div className="provider-capability-grid">
                        {CAPABILITY_OPTIONS.map((option) => (
                          <label key={option.key}>
                            <input type="checkbox" checked={Boolean(capabilities[option.key])} onChange={(event) => toggleCapability(option.key, event.target.checked)} />
                            <span>{option.label}</span>
                          </label>
                        ))}
                      </div>
                    </fieldset>
                    <div className="button-row" style={{ marginTop: "0.75rem" }}>
                      <button
                        type="button"
                        className="secondary-button"
                        disabled={
                          Boolean(busy) ||
                          !chatModel.trim() ||
                          !apiKey.trim() ||
                          !hasAdminKey
                        }
                        onClick={() => void probeCapabilities()}
                      >
                        {busy === "probe" ? "正在探测能力…" : "探测模型能力（少量额度）"}
                      </button>
                    </div>
                    {probeNote && <p className="muted">{probeNote}</p>}
                  </details>

                  <label className="provider-route-toggle provider-embedding-toggle">
                    <input type="checkbox" checked={embeddingEnabled} onChange={(event) => { setEmbeddingEnabled(event.target.checked); }} disabled={Boolean(busy)} />
                    <span>同时保存一个向量模型（可选）</span>
                  </label>

                  {embeddingEnabled && (
                    <div className="provider-field-grid">
                      <label className="field-block" style={{ gridColumn: "1 / -1" }}>
                        <span>向量接入点</span>
                        <input
                          value={embeddingBaseUrl}
                          onChange={(event) => updateEmbeddingBaseUrl(event.target.value)}
                          spellCheck={false}
                          placeholder="默认与聊天地址相同"
                          aria-label="向量接入点"
                          disabled={Boolean(busy)}
                        />
                        <p className="provider-wizard-hint">
                          默认与聊天渠道相同。若向量接口在另一个 HTTPS 地址（独立端口或工作空间），在此填写；保存时会自动建一条只跑向量的渠道并复用同一密钥。
                        </p>
                      </label>
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
                        <input value={embeddingSpace} onChange={(event) => { setEmbeddingSpace(event.target.value); setEmbeddingSpaceEdited(true); }} spellCheck={false} placeholder="不可与不同模型/维度混用" disabled={Boolean(busy)} />
                      </label>
                      <label className="field-block">
                        <span>现有向量路由</span>
                        <select
                          value={embeddingRouteOperation}
                          onChange={(event) => { setEmbeddingRouteOperation(event.target.value as ModelGatewayRouteOperation); }}
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
                  <h3><span className="provider-step-index" aria-hidden>3</span>检查并保存</h3>
                  <p className="provider-wizard-hint">
                    先检查配置，通过后再保存。检查阶段不会写入密钥；保存失败不会改现有渠道。
                  </p>
                  <div className="provider-wizard-actions">
                    <button type="button" className="secondary-button" onClick={() => void validate()} disabled={!chatModel.trim() || Boolean(busy)}>
                      <ShieldCheck size={16} aria-hidden />
                      {busy === "validate" ? "正在校验" : validated ? "重新校验" : "校验完整配置"}
                    </button>
                    <button type="button" className="primary-button" onClick={() => void apply()} disabled={!validated || Boolean(busy)}>
                      <Save size={16} aria-hidden />
                      {busy === "apply" ? "正在保存" : "确认并保存"}
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
