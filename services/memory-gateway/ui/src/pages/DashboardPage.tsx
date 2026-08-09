import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import {
  ArrowRight,
  Brain,
  CheckCircle2,
  ChevronDown,
  Download,
  GitBranch,
  Layers3,
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
import { downloadFile } from "../utils/files";
import {
  MEMORY_TYPES,
  MEMORY_TYPE_COLOR_VAR,
  SENSITIVITIES,
  SURFACE_MODES
} from "../utils/constants";
import {
  dateText,
  displayText,
  errorMessage,
  percent,
  shortId
} from "../utils/format";
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

type LoadState = {
  loading: boolean;
  error: string | null;
  data: DashboardData | null;
};

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
  refreshKey
}: {
  api: MemoryApi;
  settings: ConnectionSettings;
  setPage: (page: PageKey) => void;
  openMemory: (id: string) => void;
  notify: Notify;
  confirm: ConfirmFn;
  refreshKey: number;
}) {
  const [state, setState] = useState<LoadState>({ loading: true, error: null, data: null });
  const [surfaceLoading, setSurfaceLoading] = useState(false);
  const [networkLoading, setNetworkLoading] = useState(false);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [surfaceMode, setSurfaceMode] = useState<SurfaceMode>("balanced");
  const [networkDensity, setNetworkDensity] = useState<NetworkDensity>("overview");
  const [networkFiltersOpen, setNetworkFiltersOpen] = useState(false);
  const [networkFilters, setNetworkFilters] = useState<NetworkFilters>(DEFAULT_NETWORK_FILTERS);

  const load = useCallback(async (
    nextSurfaceMode: SurfaceMode,
    nextDensity: NetworkDensity,
    nextFilters: NetworkFilters,
    signal?: AbortSignal
  ) => {
    setState((current) => ({ ...current, loading: true, error: null }));
    try {
      const density =
        NETWORK_DENSITY_OPTIONS.find((option) => option.key === nextDensity) ||
        NETWORK_DENSITY_OPTIONS[0];
      const [health, report, review, logs, surfaced, network, spaces, workbench, providers] = await Promise.all([
        api.health(signal),
        api.memoryReport(signal),
        api.reviewMemories(signal),
        api.decisionLogs(10, {}, signal),
        api.surfaceMemories(6, nextSurfaceMode, { redactSensitive: true }, signal),
        api.memoryNetwork({
          limit: density.limit,
          similarityThreshold: 0.42,
          maxSimilarityEdges: density.maxSimilarityEdges,
          spaceId: nextFilters.spaceId === "all" ? undefined : nextFilters.spaceId,
          type: nextFilters.type === "all" ? undefined : nextFilters.type,
          sensitivity:
            nextFilters.sensitivity === "all" ? undefined : nextFilters.sensitivity,
          valenceMin: nextFilters.valenceMin,
          valenceMax: nextFilters.valenceMax,
          arousalMin: nextFilters.arousalMin,
          arousalMax: nextFilters.arousalMax,
          redactSensitive: true
        }, signal),
        api.listMemorySpaces(signal),
        api.recallEvaluationWorkbench({}, signal).catch(() => null),
        api.providersStatus(signal).catch(() => null)
      ]);
      setState({
        loading: false,
        error: null,
        data: {
          health: health.status,
          report,
          review,
          logs,
          surfaced,
          network,
          spaces,
          evalProgress: summarizeEvalProgress(workbench),
          setup: providers?.setup || null
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
    void load("balanced", "overview", DEFAULT_NETWORK_FILTERS, controller.signal);
    return () => controller.abort();
  }, [load]);

  // 全局记忆档案抽屉改动记忆后（refreshKey 递增），按当前的浮现模式与
  // 网络过滤条件重取工作室数据；挂载当次的初始加载不重复触发。
  const seenRefreshKeyRef = useRef(refreshKey);
  useEffect(() => {
    if (refreshKey === seenRefreshKeyRef.current) return;
    seenRefreshKeyRef.current = refreshKey;
    void load(surfaceMode, networkDensity, networkFilters);
  }, [refreshKey, load, surfaceMode, networkDensity, networkFilters]);

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
    } catch (error) {
      if (!isAbortError(error)) notify(errorMessage(error), "error");
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

  const actions = data ? buildStudioActions(data) : [];
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
      {state.error && !state.data && <ErrorBlock message={state.error} onRetry={() => void load(surfaceMode, networkDensity, networkFilters)} />}

      {data && (
        <>
          <header className="studio-head">
            <div className="studio-head-row">
              <div className="studio-greeting">
                <strong className="greeting-text">{greeting}</strong>
                <span className="greeting-date">{todayText}</span>
              </div>
              <div className="studio-head-side">
                <span className="studio-health">
                  <span className={`status-dot ${data.health === "ok" ? "ok" : "bad"}`} />
                  {data.health === "ok"
                    ? data.setup && !data.setup.chat_ready
                      ? "服务在线 · 模型待配置"
                      : "聊天配置已就绪"
                    : data.health}
                </span>
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
                  onClick={() => void load(surfaceMode, networkDensity, networkFilters)}
                  title="刷新数据"
                  aria-label="刷新整页数据"
                  disabled={state.loading}
                >
                  <RefreshCcw size={15} />
                </button>
              </div>
            </div>
            <span className="hero-note">
              <ShieldAlert size={13} />
              遮罩视图 · 私密与敏感正文已隐藏
            </span>
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

          <MetricStrip metrics={metrics} />

          <div className="studio-grid">
            <div className="studio-main">
            <section className="panel surfaced-panel" aria-busy={surfaceLoading}>              <div className="panel-header">
                <h2>
                  <Sparkles size={18} />
                  浮现记忆
                </h2>
                <button className="ghost-button compact" type="button" onClick={() => setPage("memories")}>
                  打开记忆库
                </button>
              </div>
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
              <VitalityTrack memories={data.surfaced} loading={surfaceLoading} />
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

            <div className="studio-side">
              <section className="panel panel--quiet emotion-panel">
              <div className="panel-header">
                <h2>
                  <Brain size={18} />
                  情绪分布
                </h2>
              </div>
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
            </div>
          </div>

          <section className="panel panel--quiet network-panel">
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
                onDeveloper={() => setPage("developer")}
              />
            </div>
          </section>
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
  onDeveloper
}: {
  node: MemoryNetworkNode | null;
  spaces: MemorySpace[];
  api: MemoryApi;
  onOpenMemory: () => void;
  onDeveloper: () => void;
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
          <button className="secondary-button compact" type="button" onClick={onDeveloper}>
            <Wrench size={15} />
            接入
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
          <dd>{spaceNamesForNode(node, spaces).join("、") || "-"}</dd>
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

function buildStudioActions(data: DashboardData): StudioAction[] {
  const list: StudioAction[] = [];
  if (data.setup && !data.setup.chat_ready) {
    list.push({
      key: "model-setup",
      tone: "warning",
      title: data.setup.state === "configuration_error" ? "模型配置需处理" : "完成首次配置",
      value: "还差一步",
      hint: "选择模型渠道并验证后，客户端才能正常聊天",
      page: "providers"
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
  if (data.evalProgress && data.evalProgress.unlabeled > 0) {
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
  if (data.report.counts.core_sections === 0) {
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

function spaceNamesForNode(node: MemoryNetworkNode, spaces: MemorySpace[]): string[] {
  const namesById = new Map(spaces.map((space) => [space.id, space.name]));
  return (node.space_ids || []).map((spaceId) => namesById.get(spaceId) || spaceId);
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
