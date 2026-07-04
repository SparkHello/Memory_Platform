import { useCallback, useEffect, useMemo, useState, type CSSProperties } from "react";
import {
  Activity,
  Archive,
  Brain,
  Database,
  FileText,
  GitBranch,
  Layers3,
  ListChecks,
  RefreshCcw,
  ShieldAlert,
  Sparkles,
  Wrench
} from "lucide-react";
import { MemoryApi } from "../api";
import { MemoryNetwork } from "../components/MemoryNetwork";
import { MemoryTraverse } from "../components/MemoryTraverse";
import { PageHeader } from "../components/PageHeader";
import { EmptyBlock, ErrorBlock, LoadingBlock } from "../components/StateBlocks";
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
  ReviewResult,
  SurfaceMode,
  TraversalResponse
} from "../types";
import { downloadFile } from "../utils/files";
import { MEMORY_TYPES, SENSITIVITIES, SURFACE_MODES } from "../utils/constants";
import {
  dateText,
  displayText,
  errorMessage,
  joinUrl,
  percent,
  shortId
} from "../utils/format";
import type { Notify } from "./pageTypes";

type DashboardData = {
  health: string;
  report: MemoryReport;
  review: ReviewResult;
  logs: DecisionLog[];
  surfaced: MemorySurfaceRecord[];
  network: MemoryNetworkData;
  spaces: MemorySpace[];
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

type LoadState = {
  loading: boolean;
  error: string | null;
  data: DashboardData | null;
};

export function DashboardPage({
  api,
  settings,
  setPage,
  notify
}: {
  api: MemoryApi;
  settings: ConnectionSettings;
  setPage: (page: PageKey) => void;
  notify: Notify;
}) {
  const [state, setState] = useState<LoadState>({ loading: true, error: null, data: null });
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [surfaceMode, setSurfaceMode] = useState<SurfaceMode>("balanced");
  const [networkDensity, setNetworkDensity] = useState<NetworkDensity>("overview");
  const [networkFilters, setNetworkFilters] = useState<NetworkFilters>({
    spaceId: "all",
    type: "all",
    sensitivity: "all",
    valenceMin: 0,
    valenceMax: 1,
    arousalMin: 0,
    arousalMax: 1
  });

  const load = useCallback(async () => {
    setState({ loading: true, error: null, data: null });
    try {
      const density =
        NETWORK_DENSITY_OPTIONS.find((option) => option.key === networkDensity) ||
        NETWORK_DENSITY_OPTIONS[0];
      const [health, report, review, logs, surfaced, network, spaces] = await Promise.all([
        api.health(),
        api.memoryReport(),
        api.reviewMemories(),
        api.decisionLogs(10),
        api.surfaceMemories(6, surfaceMode, { redactSensitive: true }),
        api.memoryNetwork({
          limit: density.limit,
          similarityThreshold: 0.42,
          maxSimilarityEdges: density.maxSimilarityEdges,
          spaceId: networkFilters.spaceId === "all" ? undefined : networkFilters.spaceId,
          type: networkFilters.type === "all" ? undefined : networkFilters.type,
          sensitivity:
            networkFilters.sensitivity === "all" ? undefined : networkFilters.sensitivity,
          valenceMin: networkFilters.valenceMin,
          valenceMax: networkFilters.valenceMax,
          arousalMin: networkFilters.arousalMin,
          arousalMax: networkFilters.arousalMax,
          redactSensitive: true
        }),
        api.listMemorySpaces()
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
          spaces
        }
      });
    } catch (error) {
      setState({ loading: false, error: errorMessage(error), data: null });
    }
  }, [api, surfaceMode, networkDensity, networkFilters]);

  useEffect(() => {
    void load();
  }, [load]);

  const data = state.data;
  const selectedNode = useMemo(() => {
    if (!data || !selectedNodeId) return null;
    return data.network.nodes.find((node) => node.id === selectedNodeId) || null;
  }, [data, selectedNodeId]);
  const emotion = useMemo(() => (data ? summarizeEmotion(data.network.nodes) : null), [data]);

  return (
    <div className="page-stack studio-page">
      <PageHeader
        title="记忆工作室"
        subtitle="让长期记忆以关系、情绪和浮现顺序被看见。"
        action={
          <button className="secondary-button" type="button" onClick={load}>
            <RefreshCcw size={16} />
            刷新
          </button>
        }
      />

      <div className="notice">
        <ShieldAlert size={16} />
        当前为遮罩视图，私密和敏感正文已隐藏。
      </div>

      {state.loading && <LoadingBlock label="正在整理记忆工作室" />}
      {state.error && <ErrorBlock message={state.error} onRetry={load} />}

      {data && (
        <>
          <section className="studio-hero">
            <div>
              <span className="studio-kicker">Memory Studio</span>
              <h2>今天最值得回到脑海里的线索</h2>
              <p>
                当前连接到 {settings.userId || "default"} 的本地记忆库，MCP 地址为{" "}
                <code>{joinUrl(settings.apiBaseUrl, "/mcp")}</code>
              </p>
            </div>
            <div className="studio-health">
              <span className={`status-dot ${data.health === "ok" ? "ok" : "bad"}`} />
              <strong>{data.health === "ok" ? "服务在线" : data.health}</strong>
            </div>
          </section>

          <div className="studio-metrics">
            <Metric icon={Database} label="活跃记忆" value={data.report.counts.active_memories} />
            <Metric icon={Archive} label="回收站" value={data.report.counts.deleted_memories} />
            <Metric icon={Layers3} label="核心分区" value={data.report.counts.core_sections} />
            <Metric icon={Layers3} label="记忆空间" value={data.spaces.length} />
            <Metric icon={ListChecks} label="体检建议" value={data.review.recommendations.length} />
            <Metric icon={Activity} label="最近决策" value={data.logs.length} />
          </div>

          <div className="studio-grid">
            <section className="panel surfaced-panel">
              <div className="panel-header">
                <h2>
                  <Sparkles size={18} />
                  浮现记忆
                </h2>
                <button className="ghost-button compact" type="button" onClick={() => setPage("memories")}>
                  打开记忆库
                </button>
              </div>
              <div className="tabs surface-mode-tabs" aria-label="浮现模式">
                {SURFACE_MODES.map((mode) => (
                  <button
                    key={mode}
                    type="button"
                    className={surfaceMode === mode ? "active" : ""}
                    onClick={() => setSurfaceMode(mode)}
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
                      className={`surfaced-item ${selectedNodeId === memory.id ? "active" : ""}`}
                      type="button"
                      onClick={() => setSelectedNodeId(memory.id)}
                    >
                      <span>{memory.surface_reason_text || surfaceReason(memory.surface_reason)}</span>
                      <strong>{memory.content}</strong>
                      <small>
                        重要度 {memory.importance} · 浮现分 {scoreText(memory.surface_score)} · 生命力{" "}
                        {scoreText(memory.life_score)}
                      </small>
                      <small>
                        最近活跃 {daysSinceText(memory.days_since_last_active)} ·{" "}
                        {memory.review_signals.length
                          ? memory.review_signals.map(displayText).join("、")
                          : "无复核信号"}
                      </small>
                    </button>
                  ))}
                </div>
              )}
            </section>

            <section className="panel emotion-panel">
              <div className="panel-header">
                <h2>
                  <Brain size={18} />
                  情绪分布
                </h2>
              </div>
              {emotion && (
                <>
                  <div
                    className="emotion-orbit"
                    style={
                      {
                        "--x": `${emotion.valence * 100}%`,
                        "--y": `${(1 - emotion.arousal) * 100}%`
                      } as CSSProperties
                    }
                  >
                    <span />
                  </div>
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
              )}
            </section>

            <section className="panel spaces-panel">
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

          <section className="panel network-panel">
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
                      onClick={() => setNetworkDensity(option.key)}
                    >
                      {option.label}
                    </button>
                  ))}
                </div>
              </div>
            </div>
            <NetworkFiltersView
              filters={networkFilters}
              spaces={data.spaces}
              onChange={setNetworkFilters}
            />
            <div className="network-workspace">
              <MemoryNetwork
                network={data.network}
                selectedId={selectedNodeId}
                onSelect={(node) => setSelectedNodeId(node.id)}
              />
              <NetworkDetail
                node={selectedNode}
                spaces={data.spaces}
                api={api}
                onOpenMemory={() => setPage("memories")}
                onExport={async () => {
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
                }}
                onDeveloper={() => setPage("developer")}
              />
            </div>
          </section>
        </>
      )}
    </div>
  );
}

function Metric({
  icon: Icon,
  label,
  value
}: {
  icon: typeof Database;
  label: string;
  value: number | string;
}) {
  return (
    <div className="studio-metric">
      <Icon size={18} />
      <span>{label}</span>
      <strong>{value}</strong>
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
            <option key={space.id} value={space.id}>
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
    </div>
  );
}

function NetworkDetail({
  node,
  spaces,
  api,
  onOpenMemory,
  onExport,
  onDeveloper
}: {
  node: MemoryNetworkNode | null;
  spaces: MemorySpace[];
  api: MemoryApi;
  onOpenMemory: () => void;
  onExport: () => void;
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
          <button className="secondary-button compact" type="button" onClick={onExport}>
            <FileText size={15} />
            导出
          </button>
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
        打开记忆库编辑
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
        实验图遍历
      </button>
      <MemoryTraverse
        traverse={traverse.data}
        loading={traverse.loading}
        error={traverse.error}
      />
    </aside>
  );
}

function summarizeEmotion(nodes: MemoryNetworkNode[]) {
  const memories = nodes.filter((node) => node.kind === "memory");
  if (!memories.length) {
    return { valence: 0.5, arousal: 0.3, positive: 0, highArousal: 0 };
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

function scoreText(value?: number | null): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "-";
  return value.toFixed(1);
}

function daysSinceText(value?: number | null): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "-";
  if (value < 1) return "今天";
  return `${Math.round(value)} 天前`;
}
