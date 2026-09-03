import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import {
  ArrowRight,
  Brain,
  CheckCircle2,
  ChevronDown,
  ClipboardCopy,
  Download,
  Eye,
  EyeOff,
  GitBranch,
  Layers3,
  LoaderCircle,
  MessageCircle,
  PlugZap,
  RefreshCcw,
  ShieldAlert,
  SlidersHorizontal,
  Sparkles,
  Wrench
} from "lucide-react";
import { MemoryApi, isAbortError } from "../api";
import { MemoryNetwork } from "../components/MemoryNetwork";
import { MemoryTraverse } from "../components/MemoryTraverse";
import { EmptyBlock, ErrorBlock, LoadingBlock } from "../components/StateBlocks";
import { useCountUp } from "../hooks/useCountUp";
import type { ConfirmFn } from "../hooks/useConfirm";
import type { LoadState } from "../hooks/useAsyncData";
import type {
  ConnectionSettings,
  DecisionLog,
  MemoryNetwork as MemoryNetworkData,
  MemoryNetworkNode,
  MemoryReport,
  MemorySensitivity,
  MemorySpace,
  MemorySurfaceRecord,
  MemoryType,
  PageKey,
  ProvidersStatus,
  ReviewResult,
  SurfaceMode,
  TraversalResponse
} from "../types";
import { friendlyIngestSkipReason } from "../utils/decisionReason";
import { downloadFile } from "../utils/files";
import { spaceNamesFor } from "../utils/memory";
import {
  CLIENT_MODEL_ID,
  MEMORY_TYPES,
  MEMORY_TYPE_COLOR_VAR,
  SENSITIVITIES,
  SURFACE_MODES
} from "../utils/constants";
import {
  clientConfigText,
  dateText,
  displayText,
  errorMessage,
  percent,
  shortId
} from "../utils/format";
import { isProviderSetupReady } from "../utils/providerSetup";
import type { Notify } from "./pageTypes";

type EvalProgress = {
  total: number;
  unlabeled: number;
  targetMin: number;
};

type DashboardData = {
  health: string;
  report: MemoryReport;
  review: ReviewResult;
  logs: DecisionLog[];
  surfaced: MemorySurfaceRecord[];
  network: MemoryNetworkData;
  spaces: MemorySpace[];
  evalProgress: EvalProgress | null;
  setup: ProvidersStatus["setup"] | null;
  legacyKeyEnabled: boolean;
  authenticatedWithLegacyKey: boolean;
  // token 列表拉取失败时为 null，避免误判"没有 chat token"而弹首登卡。
  hasChatToken: boolean | null;
};

type NetworkFilters = {
  spaceId: "all" | string;
  type: "all" | MemoryType;
  sensitivity: "all" | MemorySensitivity;
  valenceMin: number;
  valenceMax: number;
  arousalMin: number;
  arousalMax: number;
};

type NetworkDensity = "overview" | "standard" | "more";

const DEFAULT_NETWORK_FILTERS: NetworkFilters = {
  spaceId: "all",
  type: "all",
  sensitivity: "all",
  valenceMin: 0,
  valenceMax: 1,
  arousalMin: 0,
  arousalMax: 1
};

const NETWORK_DENSITY_OPTIONS: Array<{
  key: NetworkDensity;
  label: string;
  limit: number;
  maxSimilarityEdges: number;
}> = [
  { key: "overview", label: "概览", limit: 36, maxSimilarityEdges: 40 },
  { key: "standard", label: "标准", limit: 64, maxSimilarityEdges: 70 },
  { key: "more", label: "更多", limit: 110, maxSimilarityEdges: 120 }
];

type EmotionPresetKey = "all" | "positive" | "negative" | "high_arousal" | "calm";

const EMOTION_PRESETS: Array<{
  key: EmotionPresetKey;
  label: string;
  valence: [number, number];
  arousal: [number, number];
}> = [
  { key: "all", label: "全部", valence: [0, 1], arousal: [0, 1] },
  { key: "positive", label: "偏积极", valence: [0.62, 1], arousal: [0, 1] },
  { key: "negative", label: "偏低落", valence: [0, 0.38], arousal: [0, 1] },
  { key: "high_arousal", label: "高唤起", valence: [0, 1], arousal: [0.65, 1] },
  { key: "calm", label: "平静", valence: [0, 1], arousal: [0, 0.4] }
];

function emotionPresetFor(filters: NetworkFilters): EmotionPresetKey | "custom" {
  const match = EMOTION_PRESETS.find(
    (preset) =>
      preset.valence[0] === filters.valenceMin &&
      preset.valence[1] === filters.valenceMax &&
      preset.arousal[0] === filters.arousalMin &&
      preset.arousal[1] === filters.arousalMax
  );
  return match ? match.key : "custom";
}

type StudioAction = {
  key: string;
  tone: "warning" | "info" | "muted" | "primary";
  title: string;
  value: string;
  hint: string;
  page: PageKey;
  // 需要精确落地地址（如回收站 tab）时优先走 hash 跳转。
  hash?: `#/${string}`;
};

export function DashboardPage({
  api,
  settings,
  setPage,
  openMemory,
  notify,
  confirm,
  refreshKey,
  expertMode = false
}: {
  api: MemoryApi;
  settings: ConnectionSettings;
  setPage: (page: PageKey) => void;
  openMemory: (id: string) => void;
  notify: Notify;
  confirm: ConfirmFn;
  refreshKey: number;
  expertMode?: boolean;
}) {
  const [state, setState] = useState<LoadState<DashboardData>>({ loading: true, error: null, data: null });
  const [surfaceLoading, setSurfaceLoading] = useState(false);
  const [networkLoading, setNetworkLoading] = useState(false);
  const [networkLoaded, setNetworkLoaded] = useState(false);
  const [networkError, setNetworkError] = useState<string | null>(null);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [surfaceMode, setSurfaceMode] = useState<SurfaceMode>("balanced");
  const [networkDensity, setNetworkDensity] = useState<NetworkDensity>("overview");
  const [networkFiltersOpen, setNetworkFiltersOpen] = useState(false);
  const [networkFilters, setNetworkFilters] = useState<NetworkFilters>(DEFAULT_NETWORK_FILTERS);
  // 「试一下」卡在第一条记忆出现后要停留在完成态，而不是随着计数变化立刻消失。
  const [firstChatDone, setFirstChatDone] = useState(false);

  const load = useCallback(async (
    nextSurfaceMode: SurfaceMode,
    signal?: AbortSignal
  ) => {
    setState((current) => ({ ...current, loading: true, error: null }));
    try {
      const [health, report, logs, surfaced, spaces, providers, tokens] =
        await Promise.all([
          api.health(signal),
          api.memoryReport(signal),
          api.decisionLogs(10, {}, signal),
          api.surfaceMemories(6, nextSurfaceMode, { redactSensitive: true }, signal),
          api.listMemorySpaces({ signal }),
          api.providersStatus(signal).catch(() => null),
          api.authTokens(signal).catch(() => null)
        ]);
      setNetworkLoaded(false);
      setNetworkError(null);
      setSelectedNodeId(null);
      setState({
        loading: false,
        error: null,
        data: {
          health: health.status,
          report,
          review: { total: 0, recommendations: [] },
          logs,
          surfaced,
          network: {
            nodes: [],
            edges: [],
            meta: {
              memory_count: 0,
              core_count: 0,
              similarity_threshold: 0.42,
              max_similarity_edges: 0
            }
          },
          spaces,
          evalProgress: null,
          setup: providers?.setup || null,
          legacyKeyEnabled: Boolean(tokens?.legacy_key_enabled),
          authenticatedWithLegacyKey: Boolean(tokens?.authenticated_with_legacy_key),
          hasChatToken: tokens
            ? tokens.data.some(
                (record) => record.role === "chat" && !record.revoked_at
              )
            : null
        }
      });
    } catch (error) {
      // 过期请求在 cleanup 里被 abort，直接丢弃，不覆盖新结果。
      if (isAbortError(error)) return;
      setState((current) => ({ ...current, loading: false, error: errorMessage(error) }));
    }
  }, [api]);

  useEffect(() => {
    const controller = new AbortController();
    void load("balanced", controller.signal);
    return () => controller.abort();
  }, [load]);

  // 全局记忆档案抽屉改动记忆后（refreshKey 递增），按当前的浮现模式与
  // 网络过滤条件重取工作室数据；挂载当次的初始加载不重复触发。
  const seenRefreshKeyRef = useRef(refreshKey);
  useEffect(() => {
    if (refreshKey === seenRefreshKeyRef.current) return;
    seenRefreshKeyRef.current = refreshKey;
    void load(surfaceMode);
  }, [refreshKey, load, surfaceMode]);

  const surfaceRequestRef = useRef<AbortController | null>(null);
  const networkRequestRef = useRef<AbortController | null>(null);

  // 卸载时取消用户触发的局部刷新请求。
  useEffect(
    () => () => {
      surfaceRequestRef.current?.abort();
      networkRequestRef.current?.abort();
    },
    []
  );

  const refreshSurface = async (nextMode: SurfaceMode) => {
    setSurfaceMode(nextMode);
    if (!state.data) return;
    surfaceRequestRef.current?.abort();
    const controller = new AbortController();
    surfaceRequestRef.current = controller;
    setSurfaceLoading(true);
    try {
      const surfaced = await api.surfaceMemories(6, nextMode, { redactSensitive: true }, controller.signal);
      setState((current) => current.data ? {
        ...current,
        data: { ...current.data, surfaced }
      } : current);
    } catch (error) {
      if (!isAbortError(error)) notify(errorMessage(error), "error");
    } finally {
      if (surfaceRequestRef.current === controller) setSurfaceLoading(false);
    }
  };

  const refreshNetwork = async (nextDensity: NetworkDensity, nextFilters: NetworkFilters) => {
    if (!state.data) return;
    const density = NETWORK_DENSITY_OPTIONS.find((option) => option.key === nextDensity) || NETWORK_DENSITY_OPTIONS[0];
    networkRequestRef.current?.abort();
    const controller = new AbortController();
    networkRequestRef.current = controller;
    setNetworkLoading(true);
    setNetworkError(null);
    try {
      const network = await api.memoryNetwork({
        limit: density.limit,
        similarityThreshold: 0.42,
        maxSimilarityEdges: density.maxSimilarityEdges,
        spaceId: nextFilters.spaceId === "all" ? undefined : nextFilters.spaceId,
        type: nextFilters.type === "all" ? undefined : nextFilters.type,
        sensitivity: nextFilters.sensitivity === "all" ? undefined : nextFilters.sensitivity,
        valenceMin: nextFilters.valenceMin,
        valenceMax: nextFilters.valenceMax,
        arousalMin: nextFilters.arousalMin,
        arousalMax: nextFilters.arousalMax,
        redactSensitive: true
      }, controller.signal);
      setSelectedNodeId((current) => current && network.nodes.some((node) => node.id === current) ? current : null);
      setState((current) => current.data ? {
        ...current,
        data: { ...current.data, network }
      } : current);
      setNetworkLoaded(true);
    } catch (error) {
      if (!isAbortError(error)) setNetworkError(errorMessage(error));
    } finally {
      if (networkRequestRef.current === controller) setNetworkLoading(false);
    }
  };

  const changeNetworkDensity = (nextDensity: NetworkDensity) => {
    setNetworkDensity(nextDensity);
    void refreshNetwork(nextDensity, networkFilters);
  };

  const changeNetworkFilters = (nextFilters: NetworkFilters) => {
    setNetworkFilters(nextFilters);
    void refreshNetwork(networkDensity, nextFilters);
  };

  // 导出的是完整备份（含私密/敏感正文），与报告页一致先弹确认警告。
  const exportJson = async () => {
    const confirmed = await confirm({
      title: "导出 JSON 备份",
      message: "导出的 JSON 会包含完整私密/敏感正文，请妥善保管导出文件。",
      confirmLabel: "导出",
      tone: "warning"
    });
    if (!confirmed) return;
    try {
      const exportData = await api.exportMemories("json");
      downloadFile(
        `memory-export-${settings.userId}.json`,
        JSON.stringify(exportData, null, 2),
        "application/json"
      );
      notify("已生成 JSON 备份", "success");
    } catch (error) {
      notify(errorMessage(error), "error");
    }
  };

  const data = state.data;
  const selectedNode = useMemo(() => {
    if (!data || !selectedNodeId) return null;
    return data.network.nodes.find((node) => node.id === selectedNodeId) || null;
  }, [data, selectedNodeId]);
  const emotion = useMemo(() => (data ? summarizeEmotion(data.network.nodes) : null), [data]);

  const metrics = data
    ? [
        { label: "活跃记忆", value: data.report.counts.active_memories },
        { label: "回收站", value: data.report.counts.deleted_memories },
        { label: "核心分区", value: data.report.counts.core_sections },
        { label: "记忆空间", value: data.spaces.length }
      ]
    : [];

  const actions = data ? buildStudioActions(data, expertMode) : [];
  const todayText = useMemo(
    () => new Date().toLocaleDateString("zh-CN", { month: "long", day: "numeric", weekday: "long" }),
    []
  );

  const greeting = useMemo(() => {
    const hour = new Date().getHours();
    if (hour < 6) return "夜深了，记忆在安静地呼吸";
    if (hour < 11) return "早安，新的一天开始了";
    if (hour < 14) return "午安，记忆在阳光下沉淀";
    if (hour < 18) return "下午好，有些记忆正在浮现";
    if (hour < 22) return "晚上好，是时候回看今天了";
    return "夜深了，记忆在安静地呼吸";
  }, []);

  return (
    <div className="page-stack studio-page">
      {state.loading && !state.data && <LoadingBlock label="正在整理记忆工作室" />}
      {state.error && !state.data && <ErrorBlock message={state.error} onRetry={() => void load(surfaceMode)} />}

      {data && (
        <>
          <header className="studio-head">
            <div className="studio-head-row">
              <div className="studio-greeting">
                <strong className="greeting-text">{greeting}</strong>
                <span className="greeting-date">{todayText}</span>
              </div>
              <div className="studio-head-side">
                {!(
                  data.health === "ok" &&
                  (!data.setup || isProviderSetupReady(data.setup))
                ) && (
                  <span className="studio-health">
                    <span className={`status-dot ${data.health === "ok" ? "ok" : "bad"}`} />
                    {data.health === "ok"
                      ? data.setup &&
                        (data.setup.state === "configuration_error" || !data.setup.service_ready)
                        ? "服务在线 · 配置需修复"
                        : "服务在线 · 模型待配置"
                      : data.health}
                  </span>
                )}
                <button
                  className="icon-button"
                  type="button"
                  onClick={() => void exportJson()}
                  title="导出 JSON 备份（含完整敏感正文）"
                  aria-label="导出 JSON 备份"
                >
                  <Download size={15} />
                </button>
                <button
                  className={`icon-button studio-refresh ${state.loading ? "is-loading" : ""}`}
                  type="button"
                  onClick={() => void load(surfaceMode)}
                  title="刷新数据"
                  aria-label="刷新整页数据"
                  disabled={state.loading}
                >
                  <RefreshCcw size={15} />
                </button>
              </div>
            </div>
          </header>

          <section className="action-band" aria-label="今日待办">
            {actions.length === 0 ? (
              <div className="action-clear">
                <CheckCircle2 size={16} />
                <span>一切就绪，没有待处理事项。记忆会在日常对话中自然积累。</span>
              </div>
            ) : (
              actions.map((action) => (
                <button
                  key={action.key}
                  type="button"
                  className={`action-card tone-${action.tone}`}
                  onClick={() => {
                    if (action.hash) {
                      window.location.hash = action.hash.slice(1);
                    } else {
                      setPage(action.page);
                    }
                  }}
                >
                  <span className="action-title">{action.title}</span>
                  <strong className="action-value">{action.value}</strong>
                  <span className="action-hint">{action.hint}</span>
                  <ArrowRight className="action-arrow" size={15} />
                </button>
              ))
            )}
          </section>

          {data.setup && !isProviderSetupReady(data.setup) && (
            <SetupNextStepCard
              setup={data.setup}
              onConfigureModel={() => setPage("providers")}
              expertMode={expertMode}
            />
          )}

          {data.setup &&
            isProviderSetupReady(data.setup) &&
            data.setup.next_action === "connect_client" &&
            data.hasChatToken === false && (
              <ConnectClientCard api={api} settings={settings} notify={notify} setPage={setPage} />
            )}

          {data.setup &&
            isProviderSetupReady(data.setup) &&
            data.hasChatToken === true &&
            (firstChatDone ||
              (data.report.counts.active_memories === 0 && data.report.counts.deleted_memories === 0)) && (
              <FirstChatCard
                api={api}
                notify={notify}
                setPage={setPage}
                onFirstMemory={() => {
                  setFirstChatDone(true);
                  void load(surfaceMode);
                }}
              />
            )}

          <div className="studio-grid">
            <div className="studio-main">
            <section className="panel surfaced-panel" aria-busy={surfaceLoading}>
              <div className="panel-header">
                <h2>
                  <Sparkles size={18} />
                  浮现记忆
                </h2>
                <button className="ghost-button compact" type="button" onClick={() => setPage("memories")}>
                  打开记忆库
                </button>
              </div>
              <label className="surfaced-mode-select">
                <span className="sr-only">浮现模式</span>
                <select
                  value={surfaceMode}
                  disabled={surfaceLoading}
                  onChange={(event) => void refreshSurface(event.target.value as typeof surfaceMode)}
                >
                  {SURFACE_MODES.map((mode) => (
                    <option key={mode} value={mode}>{displayText(mode)}</option>
                  ))}
                </select>
              </label>
              <div className="tabs surfaced-mode-tabs" aria-label="浮现模式">
                {SURFACE_MODES.map((mode) => (
                  <button
                    key={mode}
                    type="button"
                    className={surfaceMode === mode ? "active" : ""}
                    onClick={() => void refreshSurface(mode)}
                    aria-pressed={surfaceMode === mode}
                    disabled={surfaceLoading}
                  >
                    {displayText(mode)}
                  </button>
                ))}
              </div>
              {data.surfaced.length === 0 ? (
                <EmptyBlock label="当前模式暂无可浮现的记忆" compact />
              ) : (
                <div className="surfaced-list">
                  {data.surfaced.map((memory) => (
                    <button
                      key={memory.id}
                      className="surfaced-item"
                      type="button"
                      style={{ "--tc": MEMORY_TYPE_COLOR_VAR[memory.type] } as CSSProperties}
                      onClick={() => openMemory(memory.id)}
                    >
                      <span className="surfaced-tab">{displayText(memory.type)}</span>
                      <span>
                        {memory.surface_reason_text || surfaceReason(memory.surface_reason)}
                        {memory.review_signals.length
                          ? ` · ${memory.review_signals.map(displayText).join("、")}`
                          : ""}
                      </span>
                      <strong>{memory.content}</strong>
                      <span className="surfaced-footer">
                        <i className="emo-dot" style={{ background: valenceColorVar(memory.valence) }} />
                        <span className="vita">
                          <i style={{ width: `${lifeWidth(memory.life_score)}%` }} />
                        </span>
                        <small>
                          重要 {memory.importance} · {daysSinceText(memory.days_since_last_active)}
                        </small>
                      </span>
                    </button>
                  ))}
                </div>
              )}
            </section>

            <section className="panel panel--quiet spaces-panel">
              <div className="panel-header">
                <h2>
                  <Layers3 size={18} />
                  空间概览
                </h2>
                <button className="ghost-button compact" type="button" onClick={() => setPage("memories")}>
                  整理
                </button>
              </div>
              <SpaceOverview spaces={data.spaces} onOpenMemories={() => setPage("memories")} />
            </section>
            </div>
          </div>

          <details
            className="panel studio-explore"
            onToggle={(event) => {
              if (event.currentTarget.open && !networkLoaded && !networkLoading) {
                void refreshNetwork(networkDensity, networkFilters);
              }
            }}
          >
            <summary>探索情绪、网络与计数</summary>
            <MetricStrip metrics={metrics} />
            <div className="studio-explore-grid">
              <section className="emotion-panel" aria-label="情绪分布">
                <h3>情绪分布</h3>
                {emotion ? (
                  <>
                    <EmotionQuadrant valence={emotion.valence} arousal={emotion.arousal} />
                    <div className="emotion-stats">
                      <div>
                        <span>平均正向度</span>
                        <strong>{percent(emotion.valence)}</strong>
                      </div>
                      <div>
                        <span>平均唤起度</span>
                        <strong>{percent(emotion.arousal)}</strong>
                      </div>
                      <div>
                        <span>偏积极</span>
                        <strong>{emotion.positive}</strong>
                      </div>
                      <div>
                        <span>高唤起</span>
                        <strong>{emotion.highArousal}</strong>
                      </div>
                    </div>
                  </>
                ) : (
                  <EmptyBlock label="暂无记忆节点，无法统计情绪分布" compact />
                )}
              </section>
              <VitalityTrack memories={data.surfaced} loading={surfaceLoading} />
            </div>
          <section className="network-panel">
            <div className="panel-header network-panel-header">
              <h2>记忆网络</h2>
              <div className="network-header-actions">
                <span className="muted">
                  {data.network.nodes.length} 个节点 · {data.network.edges.length} 条关系
                </span>
                <div className="tabs network-density-tabs" aria-label="网络密度">
                  {NETWORK_DENSITY_OPTIONS.map((option) => (
                    <button
                      key={option.key}
                      type="button"
                      className={networkDensity === option.key ? "active" : ""}
                      onClick={() => changeNetworkDensity(option.key)}
                      disabled={networkLoading}
                    >
                      {option.label}
                    </button>
                  ))}
                </div>
                <button
                  className={`secondary-button compact network-filter-toggle ${networkFiltersOpen ? "active" : ""}`}
                  type="button"
                  onClick={() => setNetworkFiltersOpen((current) => !current)}
                  aria-expanded={networkFiltersOpen}
                >
                  <SlidersHorizontal size={15} />
                  过滤器
                  <ChevronDown size={14} />
                </button>
              </div>
            </div>
            {networkFiltersOpen && (
              <NetworkFiltersView
                filters={networkFilters}
                spaces={data.spaces}
                onChange={changeNetworkFilters}
              />
            )}
            {networkLoading && !networkLoaded && <LoadingBlock label="正在加载记忆网络" />}
            {networkError && (
              <ErrorBlock
                message={`记忆网络加载失败：${networkError}`}
                onRetry={() => void refreshNetwork(networkDensity, networkFilters)}
              />
            )}
            {networkLoaded && !networkError && (
              <div className="network-workspace" aria-busy={networkLoading}>
                <MemoryNetwork
                  network={data.network}
                  selectedId={selectedNodeId}
                  onSelect={(node) => setSelectedNodeId(node.id)}
                />
                <NetworkDetail
                  node={selectedNode}
                  spaces={data.spaces}
                  api={api}
                  onOpenMemory={() => selectedNode && openMemory(selectedNode.id)}
                  onBrowseMemories={() => setPage("memories")}
                />
              </div>
            )}
          </section>
          </details>

        </>
      )}
    </div>
  );
}

function MetricStrip({ metrics }: { metrics: Array<{ label: string; value: number | string }> }) {
  return (
    <div className="metric-strip">
      {metrics.map((metric) => (
        <div className="metric-cell" key={metric.label}>
          {typeof metric.value === "number" ? (
            <MetricNumber value={metric.value} />
          ) : (
            <strong>{metric.value}</strong>
          )}
          <span>{metric.label}</span>
        </div>
      ))}
    </div>
  );
}

function MetricNumber({ value }: { value: number }) {
  const animated = useCountUp(value);
  return <strong>{animated}</strong>;
}

function VitalityTrack({
  memories,
  loading
}: {
  memories: MemorySurfaceRecord[];
  loading: boolean;
}) {
  const average = memories.length
    ? Math.round(memories.reduce((sum, memory) => sum + lifeWidth(memory.life_score), 0) / memories.length)
    : 0;
  const empty = !loading && memories.length === 0;
  return (
    <div className="hero-vitality" aria-label={empty ? "生命力轨道暂无数据" : `浮现记忆平均生命力 ${average}%`}>
      <span>生命力轨道</span>
      <div className="vitality-rail" aria-hidden="true">
        {memories.slice(0, 6).map((memory) => (
          <i key={memory.id} style={{ width: `${Math.max(5, lifeWidth(memory.life_score))}%` }} />
        ))}
      </div>
      <strong>{loading ? "刷新中" : memories.length ? `${average}%` : "暂无数据"}</strong>
    </div>
  );
}

function SpaceOverview({
  spaces,
  onOpenMemories
}: {
  spaces: MemorySpace[];
  onOpenMemories: () => void;
}) {
  if (!spaces.length) {
    return <EmptyBlock label="还没有空间标签" compact />;
  }
  return (
    <div className="space-overview-list">
      {spaces.slice(0, 6).map((space) => (
        <button key={space.id} type="button" onClick={onOpenMemories}>
          <strong>{space.name}</strong>
          <span>{space.active_memory_count || 0} 条记忆</span>
        </button>
      ))}
    </div>
  );
}

function NetworkFiltersView({
  filters,
  spaces,
  onChange
}: {
  filters: NetworkFilters;
  spaces: MemorySpace[];
  onChange: (filters: NetworkFilters) => void;
}) {
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const activePreset = emotionPresetFor(filters);
  return (
    <div className="network-filters">
      <label className="field-block small">
        <span>空间</span>
        <select
          value={filters.spaceId}
          onChange={(event) => onChange({ ...filters, spaceId: event.target.value })}
        >
          <option value="all">全部</option>
          {spaces.map((space) => (
            <option key={space.id} value={space.name}>
              {space.name}
            </option>
          ))}
        </select>
      </label>
      <label className="field-block small">
        <span>类型</span>
        <select
          value={filters.type}
          onChange={(event) =>
            onChange({ ...filters, type: event.target.value as NetworkFilters["type"] })
          }
        >
          <option value="all">全部</option>
          {MEMORY_TYPES.map((type) => (
            <option key={type} value={type}>
              {displayText(type)}
            </option>
          ))}
        </select>
      </label>
      <label className="field-block small">
        <span>敏感级别</span>
        <select
          value={filters.sensitivity}
          onChange={(event) =>
            onChange({
              ...filters,
              sensitivity: event.target.value as NetworkFilters["sensitivity"]
            })
          }
        >
          <option value="all">全部</option>
          {SENSITIVITIES.map((sensitivity) => (
            <option key={sensitivity} value={sensitivity}>
              {displayText(sensitivity)}
            </option>
          ))}
        </select>
      </label>
      <div className="field-block small emotion-preset-field">
        <span>情绪</span>
        <div className="tabs emotion-preset-tabs" role="group" aria-label="情绪预设">
          {EMOTION_PRESETS.map((preset) => (
            <button
              key={preset.key}
              type="button"
              className={activePreset === preset.key ? "active" : ""}
              onClick={() =>
                onChange({
                  ...filters,
                  valenceMin: preset.valence[0],
                  valenceMax: preset.valence[1],
                  arousalMin: preset.arousal[0],
                  arousalMax: preset.arousal[1]
                })
              }
            >
              {preset.label}
            </button>
          ))}
        </div>
      </div>
      <div className="field-block small network-advanced-field">
        <span aria-hidden="true">&nbsp;</span>
        <button
          className={`ghost-button compact network-advanced-toggle ${advancedOpen ? "active" : ""}`}
          type="button"
          onClick={() => setAdvancedOpen((open) => !open)}
          aria-expanded={advancedOpen}
        >
          <SlidersHorizontal size={14} />
          高级数值
          <ChevronDown size={13} />
        </button>
      </div>
      {advancedOpen && (
        <>
          <label className="field-block small">
            <span>正向度下限</span>
            <input
              type="number"
              min={0}
              max={1}
              step={0.05}
              value={filters.valenceMin}
              onChange={(event) =>
                onChange({ ...filters, valenceMin: boundedUnit(Number(event.target.value)) })
              }
            />
          </label>
          <label className="field-block small">
            <span>正向度上限</span>
            <input
              type="number"
              min={0}
              max={1}
              step={0.05}
              value={filters.valenceMax}
              onChange={(event) =>
                onChange({ ...filters, valenceMax: boundedUnit(Number(event.target.value)) })
              }
            />
          </label>
          <label className="field-block small">
            <span>唤起度下限</span>
            <input
              type="number"
              min={0}
              max={1}
              step={0.05}
              value={filters.arousalMin}
              onChange={(event) =>
                onChange({ ...filters, arousalMin: boundedUnit(Number(event.target.value)) })
              }
            />
          </label>
          <label className="field-block small">
            <span>唤起度上限</span>
            <input
              type="number"
              min={0}
              max={1}
              step={0.05}
              value={filters.arousalMax}
              onChange={(event) =>
                onChange({ ...filters, arousalMax: boundedUnit(Number(event.target.value)) })
              }
            />
          </label>
        </>
      )}
    </div>
  );
}

function NetworkDetail({
  node,
  spaces,
  api,
  onOpenMemory,
  onBrowseMemories
}: {
  node: MemoryNetworkNode | null;
  spaces: MemorySpace[];
  api: MemoryApi;
  onOpenMemory: () => void;
  onBrowseMemories: () => void;
}) {
  const [traverse, setTraverse] = useState<{
    loading: boolean;
    error: string | null;
    data: TraversalResponse | null;
  }>({
    loading: false,
    error: null,
    data: null
  });
  if (!node) {
    return (
      <aside className="network-detail empty">
        <Brain size={22} />
        <strong>节点档案</strong>
        <p>关系、正文和情绪线索会在这里展开。</p>
        <div className="button-row">
          <button className="secondary-button compact" type="button" onClick={onBrowseMemories}>
            打开记忆库
          </button>
        </div>
      </aside>
    );
  }

  if (node.kind === "core") {
    return (
      <aside className="network-detail">
        <span className="detail-eyebrow">核心记忆</span>
        <h3>{node.label}</h3>
        <p>{node.content || "暂无核心摘要"}</p>
        <dl>
          <div>
            <dt>证据记忆</dt>
            <dd>{node.evidence_memory_ids?.length || 0} 条</dd>
          </div>
          <div>
            <dt>更新时间</dt>
            <dd>{dateText(node.updated_at)}</dd>
          </div>
        </dl>
      </aside>
    );
  }

  return (
    <aside className="network-detail">
      <span className="detail-eyebrow">记忆节点 · {shortId(node.id)}</span>
      <h3>{node.label}</h3>
      <p>{node.content}</p>
      <div className="emotion-pair">
        <div>
          <span>正向度</span>
          <strong>{percent(node.valence)}</strong>
        </div>
        <div>
          <span>唤起度</span>
          <strong>{percent(node.arousal)}</strong>
        </div>
      </div>
      <dl>
        <div>
          <dt>类型</dt>
          <dd>{displayText(node.type || "")}</dd>
        </div>
        <div>
          <dt>重要度</dt>
          <dd>{node.importance ?? "-"}</dd>
        </div>
        <div>
          <dt>置信度</dt>
          <dd>{percent(node.confidence)}</dd>
        </div>
        <div>
          <dt>稳定性</dt>
          <dd>{displayText(node.stability || "")}</dd>
        </div>
        <div>
          <dt>主题</dt>
          <dd>{(node.topics || []).join("、") || "-"}</dd>
        </div>
        <div>
          <dt>实体</dt>
          <dd>{(node.entities || []).join("、") || "-"}</dd>
        </div>
        <div>
          <dt>空间</dt>
          <dd>{spaceNamesFor(node, spaces).join("、") || "-"}</dd>
        </div>
        <div>
          <dt>最近使用</dt>
          <dd>{dateText(node.last_used_at)}</dd>
        </div>
      </dl>
      {node.source_message && (
        <blockquote>
          <span>来源原文</span>
          {node.source_message}
        </blockquote>
      )}
      <button className="primary-button full-width" type="button" onClick={onOpenMemory}>
        打开记忆档案
      </button>
      <button
        className="secondary-button full-width"
        type="button"
        onClick={async () => {
          setTraverse({ loading: true, error: null, data: null });
          try {
            const result = await api.traverseMemoryNetwork(node.id, {
              redactSensitive: false
            });
            setTraverse({ loading: false, error: null, data: result });
          } catch (error) {
            setTraverse({ loading: false, error: errorMessage(error), data: null });
          }
        }}
      >
        <GitBranch size={15} />
        图遍历
      </button>
      <MemoryTraverse
        traverse={traverse.data}
        loading={traverse.loading}
        error={traverse.error}
      />
    </aside>
  );
}

function EmotionQuadrant({ valence, arousal }: { valence: number; arousal: number }) {
  const x = 14 + boundedUnit(valence) * 192;
  const y = 206 - boundedUnit(arousal) * 192;
  return (
    <div className="emotion-quadrant">
      <svg viewBox="0 0 220 220" role="img" aria-label="情绪象限分布">
        <defs>
          <clipPath id="eq-clip">
            <rect x="14" y="14" width="192" height="192" rx="10" />
          </clipPath>
        </defs>
        <g clipPath="url(#eq-clip)">
          <rect className="eq-tl" x="14" y="14" width="96" height="96" />
          <rect className="eq-tr" x="110" y="14" width="96" height="96" />
          <rect className="eq-bl" x="14" y="110" width="96" height="96" />
          <rect className="eq-br" x="110" y="110" width="96" height="96" />
        </g>
        <line className="eq-axis" x1="110" y1="14" x2="110" y2="206" />
        <line className="eq-axis" x1="14" y1="110" x2="206" y2="110" />
        <line className="eq-guide" x1={x} y1="14" x2={x} y2="206" />
        <line className="eq-guide" x1="14" y1={y} x2="206" y2={y} />
        <rect className="eq-frame" x="14" y="14" width="192" height="192" rx="10" />
        <text className="eq-label" x="22" y="30">
          紧绷 / 低落
        </text>
        <text className="eq-label" x="198" y="30" textAnchor="end">
          明亮 / 兴奋
        </text>
        <text className="eq-label" x="22" y="198">
          低落 / 平静
        </text>
        <text className="eq-label" x="198" y="198">
          明亮 / 平静
        </text>
        <circle className="eq-dot" cx={x} cy={y} r="6.5" />
      </svg>
    </div>
  );
}

function SetupNextStepCard({
  setup,
  onConfigureModel,
  expertMode
}: {
  setup: NonNullable<DashboardData["setup"]>;
  onConfigureModel: () => void;
  expertMode?: boolean;
}) {
  const isRepair = setup.next_action === "repair_model_gateway" || setup.state === "configuration_error";
  return (
    <section className="panel connect-client-panel" aria-labelledby="setup-next-step-title">
      <div className="panel-header">
        <h2 id="setup-next-step-title">
          <Wrench size={18} />
          {isRepair ? "修复模型网关配置" : "下一步：配置模型渠道"}
        </h2>
      </div>
      <p className="muted">
        {isRepair
          ? "记忆服务在线，但模型网关的配置有问题。到「模型与路由」输入管理密钥（credentials/admin.txt）并按提示修复后，才能正常聊天。"
          : "你已经登录。还差一步：到「模型与路由」输入管理密钥（安装时保存在 credentials/admin.txt；从安卓 App 打开控制台时会自动带上），添加渠道并选择聊天模型。完成后回到这里创建聊天密钥。"}
      </p>
      {!isRepair && setup.missing_chat_routes?.length > 0 && (
        <p className="muted">
          {expertMode ? (
            <>
              尚未就绪的用途路由：
              <code>{setup.missing_chat_routes.join(", ")}</code>
            </>
          ) : (
            `${setup.missing_chat_routes.length} 项用途尚未就绪`
          )}
        </p>
      )}
      <div className="provider-wizard-actions">
        <button type="button" className="primary-button" onClick={onConfigureModel}>
          <SlidersHorizontal size={16} aria-hidden />
          {isRepair ? "打开模型与路由" : "去配置模型渠道"}
        </button>
      </div>
      <ol className="muted setup-key-legend">
        <li>
          <strong>登录密钥</strong>（gateway.txt）：打开这个网页用的，你已经在用
        </li>
        <li>
          <strong>管理密钥</strong>（admin.txt）：只在「模型与路由」页输入，用来改模型渠道
        </li>
        <li>
          <strong>聊天密钥</strong>：模型就绪后在本页创建，填进 Chatbox 等聊天 App
        </li>
      </ol>
    </section>
  );
}

function ConnectClientCard({
  api,
  settings,
  notify,
  setPage
}: {
  api: MemoryApi;
  settings: ConnectionSettings;
  notify: Notify;
  setPage: (page: PageKey) => void;
}) {
  const [token, setToken] = useState<string | null>(null);
  const [showToken, setShowToken] = useState(false);
  const [creating, setCreating] = useState(false);
  const clientBaseUrl = `${settings.apiBaseUrl.replace(/\/+$/, "")}/v1`;

  const createToken = async () => {
    setCreating(true);
    try {
      const dateTag = new Date().toISOString().slice(0, 10);
      const result = await api.createAuthToken(`我的第一台设备 · ${dateTag}`, "chat");
      setToken(result.token);
      notify("聊天密钥已创建；只显示这一次，请立即填进聊天 App。", "success");
    } catch (error) {
      notify(`创建聊天密钥失败：${errorMessage(error)}`, "error");
    } finally {
      setCreating(false);
    }
  };

  const copyConfig = async () => {
    if (!token) return;
    try {
      await navigator.clipboard.writeText(clientConfigText(clientBaseUrl, token));
      notify("客户端配置已复制（含聊天密钥）；请勿分享给他人。", "success");
    } catch (error) {
      notify(
        `复制失败：${errorMessage(error)}。局域网 HTTP 页面浏览器可能禁止自动复制；请点击「显示密钥」后手动选中复制。`,
        "error"
      );
    }
  };

  return (
    <section className="panel connect-client-panel" aria-labelledby="connect-client-title">
      <div className="panel-header">
        <h2 id="connect-client-title">
          <PlugZap size={18} />
          连接你的第一个聊天 App
        </h2>
      </div>
      <p className="muted">
        模型已就绪，还没有任何聊天 App 接入。创建一把聊天密钥，把下面三行填进
        OpenAI 兼容的聊天 App（如 Chatbox、RikkaHub）即可开始积累记忆。
      </p>
      <div className="client-config-summary">
        <span><small>Base URL</small><code>{clientBaseUrl}</code></span>
        <span><small>模型名</small><code>{CLIENT_MODEL_ID}</code></span>
        <span>
          <small>API Key（聊天密钥）</small>
          {token ? (
            <code className="client-token-value">
              {showToken ? token : `${token.slice(0, 12)}…${token.slice(-4)}`}
            </code>
          ) : (
            <strong className="text-warning">未创建，点下方按钮生成</strong>
          )}
        </span>
      </div>
      <div className="provider-wizard-actions">
        {token ? (
          <>
            <button
              type="button"
              className="secondary-button"
              onClick={() => setShowToken((value) => !value)}
            >
              {showToken ? <EyeOff size={16} aria-hidden /> : <Eye size={16} aria-hidden />}
              {showToken ? "隐藏密钥" : "显示密钥"}
            </button>
            <button type="button" className="primary-button" onClick={() => void copyConfig()}>
              <ClipboardCopy size={16} aria-hidden />
              复制客户端配置
            </button>
          </>
        ) : (
          <button
            type="button"
            className="primary-button"
            onClick={() => void createToken()}
            disabled={creating}
          >
            {creating ? "正在创建…" : "创建聊天密钥"}
          </button>
        )}
      </div>
      {token && (
        <>
          <p className="muted">
            聊天密钥只显示这一次；丢失后到「客户端接入」撤销并重新创建即可。
          </p>
          <FirstChatProbe api={api} setPage={setPage} />
        </>
      )}
    </section>
  );
}

/** 让非专业用户一句话验证「记忆真的在工作」的测试对话。 */
const FIRST_MEMORY_SENTENCE = "我喜欢黑咖啡，不加糖，以后推荐咖啡时记住这一点。";
const FIRST_MEMORY_FOLLOW_UP = "我喜欢什么咖啡？";
const FIRST_MEMORY_POLL_MS = 6000;

function FirstChatCard({
  api,
  notify,
  setPage,
  onFirstMemory
}: {
  api: MemoryApi;
  notify: Notify;
  setPage: (page: PageKey) => void;
  onFirstMemory: () => void;
}) {
  return (
    <section className="panel connect-client-panel" aria-labelledby="first-chat-title">
      <div className="panel-header">
        <h2 id="first-chat-title">
          <MessageCircle size={18} />
          试一下：让 AI 记住第一件事
        </h2>
      </div>
      <p className="muted">
        聊天 App 已经接好，记忆库还是空的。发一句带个人偏好的话试试，这里会实时显示它有没有被记住。
      </p>
      <FirstChatProbe api={api} setPage={setPage} onFirstMemory={onFirstMemory} notify={notify} />
    </section>
  );
}

function FirstChatProbe({
  api,
  setPage,
  onFirstMemory,
  notify
}: {
  api: MemoryApi;
  setPage: (page: PageKey) => void;
  onFirstMemory?: () => void;
  notify?: Notify;
}) {
  const [phase, setPhase] = useState<"waiting" | "done">("waiting");
  const [skipReason, setSkipReason] = useState<string | null>(null);

  useEffect(() => {
    if (phase === "done") return;
    let cancelled = false;
    const controller = new AbortController();
    const tick = async () => {
      if (typeof document !== "undefined" && document.hidden) return;
      try {
        const report = await api.memoryReport(controller.signal);
        if (cancelled) return;
        if (report.counts.active_memories > 0) {
          setPhase("done");
          onFirstMemory?.();
          return;
        }
        const logs = await api.decisionLogs(3, {}, controller.signal);
        if (cancelled) return;
        const skipped = logs.find((log) => log.decision === "ignore");
        setSkipReason(skipped ? friendlyIngestSkipReason(skipped.reason) : null);
      } catch (error) {
        // 轮询失败静默：这只是引导卡，不该刷屏报错。
        if (isAbortError(error)) return;
      }
    };
    void tick();
    const timer = window.setInterval(() => void tick(), FIRST_MEMORY_POLL_MS);
    return () => {
      cancelled = true;
      controller.abort();
      window.clearInterval(timer);
    };
  }, [api, phase, onFirstMemory]);

  const copySentence = async () => {
    try {
      await navigator.clipboard.writeText(FIRST_MEMORY_SENTENCE);
      notify?.("测试句子已复制，去聊天 App 里发出去吧", "success");
    } catch {
      notify?.("浏览器不允许自动复制，请手动选中句子复制", "error");
    }
  };

  return (
    <div className="first-chat-probe">
      <ol className="muted first-chat-steps">
        <li>
          在聊天 App 里选模型 <code>{CLIENT_MODEL_ID}</code>，发这一句：
          <div className="first-chat-sentence">
            <q>{FIRST_MEMORY_SENTENCE}</q>
            <button type="button" className="secondary-button compact" onClick={() => void copySentence()}>
              <ClipboardCopy size={14} aria-hidden />
              复制
            </button>
          </div>
        </li>
        <li>等 AI 回答完整结束（记忆只在完整回答后保存）。</li>
        <li>
          新开一个对话问「{FIRST_MEMORY_FOLLOW_UP}」，它应该能答上来。
        </li>
      </ol>
      {phase === "done" ? (
        <div className="first-chat-status is-done" role="status">
          <CheckCircle2 size={16} aria-hidden />
          <span>第一条记忆已经保存。以后聊天时相关内容会自动带上。</span>
          <button type="button" className="ghost-button compact" onClick={() => setPage("memories")}>
            打开记忆库
          </button>
        </div>
      ) : (
        <div className="first-chat-status" role="status" aria-live="polite">
          <LoaderCircle size={16} aria-hidden className="spin" />
          <span>
            {skipReason
              ? `收到过一轮对话，但没有保存：${skipReason}。换一句带个人偏好或事实的话再试。`
              : "正在等第一条记忆出现…"}
          </span>
        </div>
      )}
    </div>
  );
}

function buildStudioActions(data: DashboardData, expertMode: boolean): StudioAction[] {
  const list: StudioAction[] = [];
  if (data.setup && !isProviderSetupReady(data.setup)) {
    list.push({
      key: "model-setup",
      tone: "warning",
      title: data.setup.state === "configuration_error" ? "模型配置需处理" : "完成首次配置",
      value: "还差一步",
      hint: "选择模型渠道并验证后，客户端才能正常聊天",
      page: "providers"
    });
  }
  if (data.legacyKeyEnabled) {
    list.push({
      key: "legacy-key",
      tone: "warning",
      title: data.authenticatedWithLegacyKey
        ? "仍在使用旧共享密钥"
        : "旧共享密钥仍启用",
      value: "需迁移",
      hint: "旧 key 同时拥有聊天、MCP 与 Console 权限；请到客户端接入改用 scoped token 后关闭 legacy",
      page: "developer"
    });
  }
  const reviewCount = data.review.recommendations.length;
  if (reviewCount > 0) {
    list.push({
      key: "review",
      tone: "warning",
      title: "记忆体检",
      value: `${reviewCount} 条`,
      hint: "有记忆值得回看，去处理体检建议",
      page: "review"
    });
  } else {
    const signalCount = data.surfaced.filter((memory) => memory.review_signals.length > 0).length;
    if (signalCount > 0) {
      list.push({
        key: "signals",
        tone: "warning",
        title: "待复核记忆",
        value: `${signalCount} 条`,
        hint: "浮现记忆带有复核信号，去体检页确认",
        page: "review"
      });
    }
  }
  // 召回标注、核心记忆整理指向评测/核心页（不在简洁导航），仅专家模式展示，避免迷路。
  if (expertMode && data.evalProgress && data.evalProgress.unlabeled > 0) {
    const graded = data.evalProgress.total - data.evalProgress.unlabeled;
    list.push({
      key: "evaluation",
      tone: "info",
      title: "召回标注",
      value: `${graded}/${data.evalProgress.targetMin}`,
      hint: `还有 ${data.evalProgress.unlabeled} 条 query 待标注`,
      page: "evaluation"
    });
  }
  if (data.report.counts.deleted_memories > 0) {
    list.push({
      key: "trash",
      tone: "muted",
      title: "回收站",
      value: `${data.report.counts.deleted_memories} 条`,
      hint: "待恢复或彻底清理",
      page: "memories",
      hash: "#/memories?tab=recycle"
    });
  }
  if (expertMode && data.report.counts.core_sections === 0) {
    list.push({
      key: "core",
      tone: "primary",
      title: "核心记忆",
      value: "未整理",
      hint: "运行一次整理，提炼长期画像",
      page: "core"
    });
  }
  return list;
}

function summarizeEvalProgress(
  workbench: { labels?: Array<{ judgment?: string; relevant_ids: string[] }>; target_label_min?: number } | null
): EvalProgress | null {
  const labels = workbench?.labels || [];
  if (!workbench || !labels.length) return null;
  const unlabeled = labels.filter((label) => {
    const judgment = label.judgment || (label.relevant_ids.length > 0 ? "relevant" : "unlabeled");
    return !(judgment === "no_answer" || (judgment === "relevant" && label.relevant_ids.length > 0));
  }).length;
  return {
    total: labels.length,
    unlabeled,
    targetMin: workbench.target_label_min || 20
  };
}

function summarizeEmotion(nodes: MemoryNetworkNode[]) {
  const memories = nodes.filter((node) => node.kind === "memory");
  // 零节点时返回 null，由上层渲染空态，不编造 50%/30% 的兜底值。
  if (!memories.length) {
    return null;
  }
  const valence = average(memories.map((node) => node.valence ?? 0.5));
  const arousal = average(memories.map((node) => node.arousal ?? 0.3));
  return {
    valence,
    arousal,
    positive: memories.filter((node) => (node.valence ?? 0.5) >= 0.62).length,
    highArousal: memories.filter((node) => (node.arousal ?? 0.3) >= 0.65).length
  };
}

function average(values: number[]): number {
  return values.reduce((sum, value) => sum + value, 0) / Math.max(1, values.length);
}

function boundedUnit(value: number): number {
  if (Number.isNaN(value)) return 0;
  return Math.min(1, Math.max(0, value));
}

function surfaceReason(reason: string): string {
  return {
    fresh_high_importance: "新近且重要",
    high_importance: "高重要度",
    high_score: "活跃度高",
    important_memory: "按重要度浮现",
    emotional_signal: "情绪信号",
    stale_important: "长期未活跃但仍重要",
    expired_review: "已经过有效期",
    review_due: "已到复核时间",
    near_expiry: "即将到期",
    emotion_uncertain: "高唤起低置信",
    sensitive_review: "敏感记忆复核",
    stale_review: "长期未活跃",
    low_life: "生命力偏低"
  }[reason] || reason;
}

function valenceColorVar(value?: number | null): string {
  const valence = value ?? 0.5;
  if (valence < 0.38) return "var(--emo-neg)";
  if (valence > 0.62) return "var(--emo-pos)";
  return "var(--emo-mid)";
}

function lifeWidth(value?: number | null): number {
  if (value === null || value === undefined || Number.isNaN(value)) return 0;
  return Math.min(100, Math.max(0, value));
}

function daysSinceText(value?: number | null): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "-";
  if (value < 1) return "今天";
  return `${Math.round(value)} 天前`;
}
