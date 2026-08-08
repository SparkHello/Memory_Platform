import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  CircleHelp,
  Database,
  History,
  Layers3,
  MessageSquareWarning,
  RefreshCcw,
  Search,
  XCircle
} from "lucide-react";
import { MemoryApi, isAbortError } from "../../api";
import { PageHeader } from "../../components/PageHeader";
import { EmptyBlock, ErrorBlock, LoadingBlock } from "../../components/StateBlocks";
import type {
  MemoryContextExplainResult,
  MemoryScoreBreakdown,
  MemorySearchRecord,
  SearchFeedbackValue
} from "../../types";
import { clampNumber, dateText, displayText, errorMessage, shortId } from "../../utils/format";
import type { Notify } from "../pageTypes";

type ExplainState = {
  loading: boolean;
  error: string | null;
  data: MemoryContextExplainResult | null;
};

const SCORE_ROWS: Array<[keyof MemoryScoreBreakdown, string]> = [
  ["semantic_score", "语义"],
  ["keyword_score", "关键词"],
  ["importance_score", "重要度"],
  ["recency_score", "新近"],
  ["usage_score", "使用"],
  ["emotion_score", "情绪"],
  ["final_score", "最终"]
];

const FEEDBACK_OPTIONS: Array<{
  value: SearchFeedbackValue;
  label: string;
  icon: typeof CheckCircle2;
}> = [
  { value: "useful", label: "有用", icon: CheckCircle2 },
  { value: "not_useful", label: "不相关", icon: XCircle },
  { value: "wrong", label: "错误", icon: AlertTriangle },
  { value: "missing", label: "缺失", icon: CircleHelp }
];

export function RecallExplainPage({
  api,
  notify,
  openMemory
}: {
  api: MemoryApi;
  notify: Notify;
  openMemory: (id: string) => void;
}) {
  const [query, setQuery] = useState("");
  const [limit, setLimit] = useState(5);
  const [includeCoreMemory, setIncludeCoreMemory] = useState(true);
  const [includeRecentContext, setIncludeRecentContext] = useState(true);
  const [state, setState] = useState<ExplainState>({
    loading: false,
    error: null,
    data: null
  });
  const [feedbackPending, setFeedbackPending] = useState<string | null>(null);
  const [feedbackRecorded, setFeedbackRecorded] = useState<Record<string, SearchFeedbackValue>>({});
  const explainRequestRef = useRef<AbortController | null>(null);

  // 卸载时取消在途的 explain 请求；重复提交时取消上一次。
  useEffect(() => () => explainRequestRef.current?.abort(), []);

  const runExplain = useCallback(async () => {
    explainRequestRef.current?.abort();
    const controller = new AbortController();
    explainRequestRef.current = controller;
    setState((current) => ({ ...current, loading: true, error: null }));
    setFeedbackRecorded({});
    try {
      const data = await api.explainContext({
        query,
        limit,
        includeCoreMemory,
        includeRecentContext,
        redactSensitive: true
      }, controller.signal);
      setState({ loading: false, error: null, data });
    } catch (error) {
      if (isAbortError(error)) return;
      setState({ loading: false, error: errorMessage(error), data: null });
    }
  }, [api, includeCoreMemory, includeRecentContext, limit, query]);

  const submitFeedback = async (memory: MemorySearchRecord, feedback: SearchFeedbackValue) => {
    const key = `${memory.id}:${feedback}`;
    setFeedbackPending(key);
    try {
      await api.submitSearchFeedback({
        query: state.data?.context_package.query || query,
        memoryId: memory.id,
        feedback
      });
      notify(`召回反馈已记录：${feedbackLabel(feedback)}`, "success");
      setFeedbackRecorded((current) => ({ ...current, [memory.id]: feedback }));
    } catch (error) {
      notify(errorMessage(error), "error");
    } finally {
      setFeedbackPending(null);
    }
  };

  const onSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    void runExplain();
  };

  const data = state.data;

  return (
    <div className="page-stack recall-page">
      <PageHeader
        title="召回解释"
        subtitle="查看一次上下文构建中被选中、候选和落选的记忆。"
        action={
          data && (
            <button className="secondary-button" type="button" onClick={() => void runExplain()}>
              <RefreshCcw size={16} />
              刷新
            </button>
          )
        }
      />

      <section className="panel recall-query-panel">
        <form className="recall-query-form" onSubmit={onSubmit}>
          <label className="field-block recall-query-input">
            <span>查询</span>
            <textarea
              value={query}
              rows={3}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="例如：咖啡偏好、当前项目、沟通风格"
            />
          </label>
          <label className="field-block">
            <span>数量</span>
            <input
              type="number"
              min={1}
              max={20}
              value={limit}
              onChange={(event) => setLimit(clampNumber(Number(event.target.value), 1, 20))}
            />
          </label>
          <div className="recall-toggles">
            <label className="checkbox-row">
              <input
                type="checkbox"
                checked={includeCoreMemory}
                onChange={(event) => setIncludeCoreMemory(event.target.checked)}
              />
              核心记忆
            </label>
            <label className="checkbox-row">
              <input
                type="checkbox"
                checked={includeRecentContext}
                onChange={(event) => setIncludeRecentContext(event.target.checked)}
              />
              近期上下文
            </label>
          </div>
          <button className="primary-button" type="submit" disabled={state.loading || !query.trim()}>
            <Search size={16} />
            解释召回
          </button>
        </form>
      </section>

      {state.loading && <LoadingBlock label="正在解释召回链路" />}
      {state.error && <ErrorBlock message={state.error} onRetry={() => void runExplain()} />}
      {!state.loading && !state.error && !data && <EmptyBlock label="输入查询后查看召回链路" />}

      {data && (
        <>
          <div className="notice">
            <AlertTriangle size={16} />
            当前为遮罩视图，私密和敏感正文已从召回说明中隐藏。
          </div>

          <section className="recall-summary-grid">
            <SummaryTile
              icon={Layers3}
              label="核心记忆"
              value={data.core_memory.length}
            />
            <SummaryTile
              icon={Database}
              label="搜索命中"
              value={data.search_results.length}
            />
            <SummaryTile
              icon={History}
              label="近期摘要"
              value={data.recent_context.found ? 1 : 0}
            />
            <SummaryTile
              icon={MessageSquareWarning}
              label="落选候选"
              value={data.excluded_candidates.length}
            />
          </section>

          <section className="panel">
            <div className="panel-header">
              <h2>最终上下文包</h2>
              <span className="muted">{data.context_package.query || "无查询"}</span>
            </div>
            <div className="recall-context-grid">
              <ContextBlock
                title="核心记忆"
                empty="未注入核心记忆"
                items={data.core_memory.map((section) => ({
                  id: section.id,
                  title: displayText(section.section),
                  body: section.content
                }))}
              />
              <ContextBlock
                title="长期记忆"
                empty="没有搜索命中"
                items={data.search_results.map((memory) => ({
                  id: memory.id,
                  title: `${shortId(memory.id)} · ${displayText(memory.type)}`,
                  body: memory.content
                }))}
              />
              <ContextBlock
                title="近期上下文"
                empty="未注入近期摘要"
                items={
                  data.recent_context.found
                    ? [{ id: "recent-context", title: "近期摘要", body: data.recent_context.summary }]
                    : []
                }
              />
            </div>
          </section>

          <section className="panel">
            <div className="panel-header">
              <h2>搜索命中</h2>
              <span className="muted">{data.search_results.length} 条进入上下文</span>
            </div>
            {data.search_results.length === 0 ? (
              <EmptyBlock label="没有命中的长期记忆" compact />
            ) : (
              <div className="recall-hit-list">
                {data.search_results.map((memory) => (
                  <MemoryHitCard
                    key={memory.id}
                    memory={memory}
                    feedbackPending={feedbackPending}
                    feedbackRecorded={feedbackRecorded[memory.id]}
                    onFeedback={submitFeedback}
                    onOpen={openMemory}
                  />
                ))}
              </div>
            )}
          </section>

          <section className="panel">
            <div className="panel-header">
              <h2>候选池</h2>
              <span className="muted">{data.candidate_pool.length} 条候选</span>
            </div>
            <CompactHitList items={data.candidate_pool} empty="暂无候选" onOpen={openMemory} />
          </section>

          <section className="panel">
            <div className="panel-header">
              <h2>被排除候选</h2>
              <span className="muted">{data.excluded_candidates.length} 条未进入上下文</span>
            </div>
            <CompactHitList items={data.excluded_candidates} empty="暂无被排除候选" showReason onOpen={openMemory} />
          </section>
        </>
      )}
    </div>
  );
}

function SummaryTile({
  icon: Icon,
  label,
  value
}: {
  icon: typeof Layers3;
  label: string;
  value: number;
}) {
  return (
    <div className="studio-metric recall-summary-tile">
      <Icon size={18} />
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function ContextBlock({
  title,
  empty,
  items
}: {
  title: string;
  empty: string;
  items: Array<{ id: string; title: string; body: string }>;
}) {
  return (
    <div className="recall-context-block">
      <h3>{title}</h3>
      {items.length === 0 ? (
        <p className="muted">{empty}</p>
      ) : (
        <div className="recall-context-items">
          {items.map((item) => (
            <div className="recall-context-item" key={item.id}>
              <span>{item.title}</span>
              <p>{item.body}</p>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function MemoryHitCard({
  memory,
  feedbackPending,
  feedbackRecorded,
  onFeedback,
  onOpen
}: {
  memory: MemorySearchRecord;
  feedbackPending: string | null;
  feedbackRecorded: SearchFeedbackValue | undefined;
  onFeedback: (memory: MemorySearchRecord, feedback: SearchFeedbackValue) => void;
  onOpen: (id: string) => void;
}) {
  return (
    <article className="recall-hit">
      <div className="recall-hit-main">
        <div className="recall-hit-header">
          <button className="hit-id-button" type="button" onClick={() => onOpen(memory.id)} title="打开记忆档案">
            {shortId(memory.id)}
          </button>
          <strong>{displayText(memory.type)}</strong>
          <em>{scoreText(memory.score_breakdown.final_score)}</em>
        </div>
        <p>{memory.content}</p>
        <div className="recall-hit-meta">
          <span>通道 {memory.channels.map(displayText).join("、") || "-"}</span>
          <span>重要度 {memory.importance}</span>
          <span>最近使用 {dateText(memory.last_used_at)}</span>
        </div>
      </div>
      <ScoreBars breakdown={memory.score_breakdown} />
      <div className="recall-feedback-row">
        {FEEDBACK_OPTIONS.map((option) => {
          const Icon = option.icon;
          const pending = feedbackPending === `${memory.id}:${option.value}`;
          const recorded = feedbackRecorded !== undefined;
          const chosen = feedbackRecorded === option.value;
          return (
            <button
              className="ghost-button compact"
              type="button"
              key={option.value}
              disabled={pending || recorded}
              onClick={() => onFeedback(memory, option.value)}
            >
              <Icon size={14} />
              {pending ? "记录中" : chosen ? `${option.label} · 已记录` : option.label}
            </button>
          );
        })}
      </div>
    </article>
  );
}

function ScoreBars({ breakdown }: { breakdown: MemoryScoreBreakdown }) {
  return (
    <div className="score-bars">
      {SCORE_ROWS.map(([key, label]) => {
        const value = clampNumber(Number(breakdown[key] || 0), 0, 100);
        return (
          <div className="score-row" key={key}>
            <span>{label}</span>
            <div className="score-track" aria-hidden="true">
              <i style={{ width: `${value}%` }} />
            </div>
            <strong>{scoreText(value)}</strong>
          </div>
        );
      })}
    </div>
  );
}

function CompactHitList({
  items,
  empty,
  showReason = false,
  onOpen
}: {
  items: MemorySearchRecord[];
  empty: string;
  showReason?: boolean;
  onOpen: (id: string) => void;
}) {
  if (items.length === 0) {
    return <EmptyBlock label={empty} compact />;
  }
  return (
    <div className="compact-hit-list">
      {items.map((memory) => (
        <button
          className="compact-hit compact-hit-button"
          type="button"
          key={`${memory.id}-${memory.excluded_reason || "candidate"}`}
          onClick={() => onOpen(memory.id)}
          title="打开记忆档案"
        >
          <span>{shortId(memory.id)}</span>
          <p>{memory.content}</p>
          <strong>{scoreText(memory.score_breakdown.final_score)}</strong>
          {showReason && <em>{displayText(memory.excluded_reason || "rank_below_limit")}</em>}
        </button>
      ))}
    </div>
  );
}

function scoreText(value: number): string {
  return Math.round(clampNumber(value, 0, 100)).toString();
}

function feedbackLabel(value: SearchFeedbackValue): string {
  return {
    useful: "有用",
    not_useful: "不相关",
    wrong: "错误",
    missing: "缺失"
  }[value];
}
