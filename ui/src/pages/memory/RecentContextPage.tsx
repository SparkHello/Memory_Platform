import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  ChevronDown,
  GitBranch,
  GitFork,
  History,
  MessageSquareText,
  RefreshCcw,
  Search,
  Trash2
} from "lucide-react";
import { MemoryApi, isAbortError } from "../../api";
import { EmptyBlock, ErrorBlock, LoadingBlock } from "../../components/StateBlocks";
import { PageHeader } from "../../components/PageHeader";
import type { ConfirmFn } from "../../hooks/useConfirm";
import type {
  ConversationBranchList,
  ConversationBranchNode,
  RecentContextSummary
} from "../../types";
import { dateText, errorMessage, shortId } from "../../utils/format";
import type { Notify } from "../pageTypes";

type ContextData = {
  branches: ConversationBranchList;
  summaries: RecentContextSummary[];
};

type ContextState = {
  loading: boolean;
  error: string | null;
  data: ContextData | null;
};

type ContextTab = "branches" | "summaries";
type BranchStatus = "active" | "archived";

type BranchTree = {
  roots: ConversationBranchNode[];
  children: Map<string, ConversationBranchNode[]>;
  childCounts: Map<string, number>;
  loadedFingerprints: Set<string>;
};

export function RecentContextPage({
  api,
  notify,
  confirm
}: {
  api: MemoryApi;
  notify: Notify;
  confirm: ConfirmFn;
}) {
  const [state, setState] = useState<ContextState>({
    loading: true,
    error: null,
    data: null
  });
  const [tab, setTab] = useState<ContextTab>("branches");
  const [branchStatus, setBranchStatus] = useState<BranchStatus>("active");
  const [query, setQuery] = useState("");
  const [limit, setLimit] = useState(500);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [mutatingId, setMutatingId] = useState<string | null>(null);

  const load = useCallback(async (signal?: AbortSignal) => {
    setState((current) => ({ ...current, loading: true, error: null }));
    try {
      const [branches, summaries] = await Promise.all([
        api.conversationBranches(limit, branchStatus, signal),
        api.recentContext(signal)
      ]);
      setState({ loading: false, error: null, data: { branches, summaries } });
      setExpanded((current) => {
        const loadedIds = new Set(branches.data.map((node) => node.id));
        const retained = new Set([...current].filter((id) => loadedIds.has(id)));
        if (retained.size > 0 || branches.data.length === 0) return retained;
        return newestBranchPath(branches.data);
      });
    } catch (error) {
      if (isAbortError(error)) return;
      setState((current) => ({
        loading: false,
        error: errorMessage(error),
        data: current.data
      }));
    }
  }, [api, branchStatus, limit]);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const allNodes = state.data?.branches.data || [];
  const visibleNodes = useMemo(
    () => filterBranchesWithAncestors(allNodes, query),
    [allNodes, query]
  );
  const tree = useMemo(() => buildBranchTree(visibleNodes, allNodes), [allNodes, visibleNodes]);
  const forkCount = useMemo(
    () =>
      allNodes.filter(
        (node) => (tree.childCounts.get(node.history_fingerprint) || 0) > 1
      ).length,
    [allNodes, tree.childCounts]
  );
  const searching = Boolean(query.trim());
  const effectiveExpanded = useMemo(
    () => searching ? new Set(visibleNodes.map((node) => node.id)) : expanded,
    [expanded, searching, visibleNodes]
  );

  const toggleExpanded = (id: string, open: boolean) => {
    setExpanded((current) => {
      const next = new Set(current);
      if (open) next.add(id);
      else next.delete(id);
      return next;
    });
  };

  const archiveBranch = async (node: ConversationBranchNode) => {
    const loadedDescendants = countLoadedDescendants(node, tree.children);
    const ok = await confirm({
      title: "清理这条对话分支",
      message: (
        <span>
          将停止召回此节点及其全部后续分支
          {loadedDescendants > 0 ? `（当前已加载 ${loadedDescendants} 个后代）` : ""}。
          这不会删除长期记忆，也不会改动 FLIT 中的聊天记录。
        </span>
      ),
      confirmLabel: "清理分支",
      tone: "warning"
    });
    if (!ok) return;

    setMutatingId(node.id);
    try {
      const result = await api.archiveConversationBranch(node.id);
      notify(`已清理 ${result.archived_count} 个上下文节点`, "success");
      setExpanded((current) => {
        const next = new Set(current);
        next.delete(node.id);
        return next;
      });
      await load();
    } catch (error) {
      notify(errorMessage(error), "error");
    } finally {
      setMutatingId(null);
    }
  };

  const restoreBranch = async (node: ConversationBranchNode) => {
    setMutatingId(node.id);
    try {
      const result = await api.restoreConversationBranch(node.id);
      notify(`已恢复 ${result.restored_count} 个上下文节点`, "success");
      setExpanded((current) => {
        const next = new Set(current);
        next.delete(node.id);
        return next;
      });
      await load();
    } catch (error) {
      notify(errorMessage(error), "error");
    } finally {
      setMutatingId(null);
    }
  };

  const branchMeta = state.data?.branches.meta;
  const summaries = state.data?.summaries || [];

  return (
    <div className="page-stack conversation-context-page">
      <PageHeader
        title="对话上下文"
        subtitle="查看网关为每条对话路线保存的滚动上下文；它们不属于长期记忆或核心记忆。"
        action={
          <button
            className="secondary-button"
            type="button"
            onClick={() => void load()}
            disabled={state.loading}
          >
            <RefreshCcw size={16} />
            刷新
          </button>
        }
      />

      <div className="context-page-toolbar">
        <div className="tabs" role="tablist" aria-label="上下文类型">
          <button
            type="button"
            role="tab"
            aria-selected={tab === "branches"}
            className={tab === "branches" ? "active" : ""}
            onClick={() => setTab("branches")}
          >
            自动分支 {branchMeta ? branchMeta.total : 0}
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={tab === "summaries"}
            className={tab === "summaries" ? "active" : ""}
            onClick={() => setTab("summaries")}
          >
            会话摘要 {summaries.length}
          </button>
        </div>
        {tab === "branches" && (
          <div className="context-selectors">
            <label className="context-limit-field">
              <span>状态</span>
              <select
                value={branchStatus}
                onChange={(event) => setBranchStatus(event.target.value as BranchStatus)}
              >
                <option value="active">正在使用</option>
                <option value="archived">已清理</option>
              </select>
            </label>
            <label className="context-limit-field">
              <span>加载</span>
              <select value={limit} onChange={(event) => setLimit(Number(event.target.value))}>
                <option value={100}>最近 100 个</option>
                <option value={500}>最近 500 个</option>
                <option value={1000}>最近 1000 个</option>
              </select>
            </label>
          </div>
        )}
      </div>

      {state.loading && !state.data && <LoadingBlock label="正在加载对话上下文" />}
      {state.error && !state.data && <ErrorBlock message={state.error} onRetry={() => void load()} />}
      {state.error && state.data && (
        <div className="notice warning">
          <AlertTriangle size={16} />
          {state.error}
        </div>
      )}

      {state.data && tab === "branches" && (
        <>
          <section className="context-overview" aria-label="分支概览">
            <ContextMetric
              label={branchStatus === "active" ? "有效节点" : "已清理节点"}
              value={branchMeta?.total || 0}
            />
            <ContextMetric label="当前加载" value={allNodes.length} />
            <ContextMetric label="可见根节点" value={tree.roots.length} />
            <ContextMetric label="分叉点" value={forkCount} />
          </section>

          <section className="panel branch-workspace">
            <div className="branch-toolbar">
              <label className="search-box branch-search">
                <Search size={16} aria-hidden="true" />
                <span className="sr-only">搜索上下文</span>
                <input
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder="搜索摘要、最近原文或对话 ID"
                />
              </label>
              <div className="button-row">
                <button
                  className="ghost-button compact"
                  type="button"
                  onClick={() => setExpanded(new Set(visibleNodes.map((node) => node.id)))}
                  disabled={searching || visibleNodes.length === 0}
                >
                  展开全部
                </button>
                <button
                  className="ghost-button compact"
                  type="button"
                  onClick={() => setExpanded(new Set())}
                  disabled={searching || expanded.size === 0}
                >
                  收起全部
                </button>
              </div>
            </div>

            <div className="notice branch-legend">
              <GitFork size={16} />
              这里显示的是持久化结构状态；请求响应头中的 matched/fork 只描述当时那一次匹配。
            </div>

            {branchMeta?.truncated && (
              <div className="notice warning">
                <AlertTriangle size={16} />
                当前只加载最近 {branchMeta.returned} 个节点，较早父节点可能显示为“未加载”。
              </div>
            )}

            {allNodes.length === 0 ? (
              <EmptyBlock
                label={
                  branchStatus === "active"
                    ? "暂无自动分支上下文"
                    : "没有已清理的分支"
                }
                hint={
                  branchStatus === "active"
                    ? "通过 /v1 完成一次包含最终回答的聊天后，分支会在后台写入并出现在这里。"
                    : "清理操作采用软删除；被清理的分支会出现在这里并可恢复。"
                }
              />
            ) : visibleNodes.length === 0 ? (
              <EmptyBlock
                label="没有匹配的上下文"
                hint="可以搜索摘要、最近两轮原文、节点编号或对话 ID。"
                action={{ label: "清除搜索", onClick: () => setQuery("") }}
              />
            ) : (
              <div className="branch-tree" role="tree" aria-label="对话上下文分支树">
                {tree.roots.map((node) => (
                  <BranchNodeView
                    key={node.id}
                    node={node}
                    tree={tree}
                    expanded={effectiveExpanded}
                    status={branchStatus}
                    mutatingId={mutatingId}
                    onToggle={toggleExpanded}
                    onArchive={archiveBranch}
                    onRestore={restoreBranch}
                  />
                ))}
              </div>
            )}
          </section>
        </>
      )}

      {state.data && tab === "summaries" && (
        <section className="panel summary-workspace">
          <div className="panel-header">
            <div>
              <h2>按会话 ID 保存的摘要</h2>
              <p className="muted">
                主要供会发送动态 conversation ID 的客户端和 MCP 使用；FLIT 通常使用上方的自动分支。
              </p>
            </div>
          </div>
          {summaries.length === 0 ? (
            <EmptyBlock
              label="暂无会话摘要"
              hint="这不代表自动分支为空，请切换到“自动分支”查看 FLIT 上下文。"
            />
          ) : (
            <div className="context-list">
              {summaries.map((item) => (
                <article className="context-summary-record" key={item.id}>
                  <div className="panel-header">
                    <h2>{item.conversation_id || "未命名对话"}</h2>
                    <span className="muted">{dateText(item.updated_at)}</span>
                  </div>
                  <p>{item.summary}</p>
                  <div className="context-record-meta">
                    <span>{item.turn_count || 0} 轮</span>
                    <span>创建于 {dateText(item.created_at)}</span>
                  </div>
                </article>
              ))}
            </div>
          )}
        </section>
      )}
    </div>
  );
}

function ContextMetric({ label, value }: { label: string; value: number }) {
  return (
    <div>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function BranchNodeView({
  node,
  tree,
  expanded,
  status,
  mutatingId,
  onToggle,
  onArchive,
  onRestore
}: {
  node: ConversationBranchNode;
  tree: BranchTree;
  expanded: Set<string>;
  status: BranchStatus;
  mutatingId: string | null;
  onToggle: (id: string, open: boolean) => void;
  onArchive: (node: ConversationBranchNode) => void;
  onRestore: (node: ConversationBranchNode) => void;
}) {
  const children = tree.children.get(node.history_fingerprint) || [];
  const childCount = tree.childCounts.get(node.history_fingerprint) || 0;
  const hasKnownParent =
    Boolean(node.parent_history_fingerprint) &&
    tree.loadedFingerprints.has(node.parent_history_fingerprint);
  const open = expanded.has(node.id);
  const latestTurn = node.recent_turns.at(-1);

  return (
    <details
      className="branch-node"
      open={open}
      onToggle={(event) => onToggle(node.id, event.currentTarget.open)}
    >
      <summary>
        <span className="branch-toggle" aria-hidden="true">
          <ChevronDown size={16} />
        </span>
        <span className="branch-node-icon" aria-hidden="true">
          {childCount > 1 ? <GitFork size={17} /> : <GitBranch size={17} />}
        </span>
        <span className="branch-node-heading">
          <strong>{latestTurn?.user || firstMeaningfulLine(node.summary) || "无标题上下文"}</strong>
          <span>
            {node.turn_count} 轮 · {dateText(node.updated_at)} · {shortId(node.id)}
          </span>
        </span>
        <span className="branch-statuses">
          {!node.parent_history_fingerprint && <span className="context-chip">根节点</span>}
          {node.parent_history_fingerprint && !hasKnownParent && (
            <span className="context-chip warning">父节点未加载</span>
          )}
          {childCount > 1 && <span className="context-chip fork">分叉点 · {childCount}</span>}
          {childCount === 1 && <span className="context-chip">续接</span>}
          {childCount === 0 && <span className="context-chip leaf">叶节点</span>}
        </span>
      </summary>

      <div className="branch-node-body">
        <div className="branch-context-grid">
          <section>
            <h3>滚动摘要</h3>
            <p className="branch-summary">{node.summary || "暂无摘要"}</p>
          </section>
          {node.compressed_summary && (
            <section>
              <h3>较早内容压缩</h3>
              <p className="branch-summary">{node.compressed_summary}</p>
            </section>
          )}
        </div>

        <section className="branch-turn-section">
          <div className="branch-section-heading">
            <h3>最近原文</h3>
            <span>{node.recent_turns.length} 轮逐字保留</span>
          </div>
          {node.recent_turns.length === 0 ? (
            <p className="muted">这个节点没有保留最近原文。</p>
          ) : (
            <div className="branch-turns">
              {node.recent_turns.map((turn, index) => (
                <div className="branch-turn" key={`${node.id}-${index}`}>
                  <div>
                    <span>用户</span>
                    <p>{turn.user}</p>
                  </div>
                  <div>
                    <span>助手</span>
                    <p>{turn.assistant}</p>
                  </div>
                </div>
              ))}
            </div>
          )}
        </section>

        <div className="branch-node-footer">
          <div className="branch-identifiers">
            <span>历史指纹 {shortFingerprint(node.history_fingerprint)}</span>
            {node.conversation_id && <span>对话 ID {node.conversation_id}</span>}
          </div>
          {status === "active" ? (
            <button
              className="danger-button compact"
              type="button"
              onClick={() => void onArchive(node)}
              disabled={mutatingId !== null}
            >
              <Trash2 size={14} />
              {mutatingId === node.id ? "正在清理" : "清理此分支"}
            </button>
          ) : (
            <button
              className="secondary-button compact"
              type="button"
              onClick={() => void onRestore(node)}
              disabled={mutatingId !== null}
            >
              <History size={14} />
              {mutatingId === node.id ? "正在恢复" : "恢复此分支"}
            </button>
          )}
        </div>
      </div>

      {open && children.length > 0 && (
        <div className="branch-children" role="group">
          {children.map((child) => (
            <BranchNodeView
              key={child.id}
              node={child}
              tree={tree}
              expanded={expanded}
              status={status}
              mutatingId={mutatingId}
              onToggle={onToggle}
              onArchive={onArchive}
              onRestore={onRestore}
            />
          ))}
        </div>
      )}
    </details>
  );
}

function buildBranchTree(
  visibleNodes: ConversationBranchNode[],
  allNodes: ConversationBranchNode[]
): BranchTree {
  const visibleFingerprints = new Set(visibleNodes.map((node) => node.history_fingerprint));
  const loadedFingerprints = new Set(allNodes.map((node) => node.history_fingerprint));
  const children = new Map<string, ConversationBranchNode[]>();
  const childCounts = new Map<string, number>();

  for (const node of allNodes) {
    if (!node.parent_history_fingerprint) continue;
    childCounts.set(
      node.parent_history_fingerprint,
      (childCounts.get(node.parent_history_fingerprint) || 0) + 1
    );
  }

  for (const node of visibleNodes) {
    if (!node.parent_history_fingerprint || !visibleFingerprints.has(node.parent_history_fingerprint)) {
      continue;
    }
    const siblings = children.get(node.parent_history_fingerprint) || [];
    siblings.push(node);
    children.set(node.parent_history_fingerprint, siblings);
  }

  const byNewest = (left: ConversationBranchNode, right: ConversationBranchNode) =>
    right.updated_at.localeCompare(left.updated_at);
  for (const siblings of children.values()) siblings.sort(byNewest);

  return {
    roots: visibleNodes
      .filter(
        (node) =>
          !node.parent_history_fingerprint ||
          !visibleFingerprints.has(node.parent_history_fingerprint)
      )
      .sort(byNewest),
    children,
    childCounts,
    loadedFingerprints
  };
}

function filterBranchesWithAncestors(
  nodes: ConversationBranchNode[],
  rawQuery: string
): ConversationBranchNode[] {
  const query = rawQuery.trim().toLocaleLowerCase();
  if (!query) return nodes;

  const byFingerprint = new Map(nodes.map((node) => [node.history_fingerprint, node]));
  const included = new Set<string>();

  for (const node of nodes) {
    const searchable = [
      node.id,
      node.conversation_id || "",
      node.summary,
      node.compressed_summary,
      ...node.recent_turns.flatMap((turn) => [turn.user, turn.assistant])
    ]
      .join("\n")
      .toLocaleLowerCase();
    if (!searchable.includes(query)) continue;

    let current: ConversationBranchNode | undefined = node;
    const visited = new Set<string>();
    while (current && !visited.has(current.history_fingerprint)) {
      included.add(current.history_fingerprint);
      visited.add(current.history_fingerprint);
      current = byFingerprint.get(current.parent_history_fingerprint);
    }
  }

  return nodes.filter((node) => included.has(node.history_fingerprint));
}

function newestBranchPath(nodes: ConversationBranchNode[]): Set<string> {
  if (nodes.length === 0) return new Set();
  const byFingerprint = new Map(nodes.map((node) => [node.history_fingerprint, node]));
  const expanded = new Set<string>();
  let current: ConversationBranchNode | undefined = nodes[0];
  const visited = new Set<string>();

  while (current && !visited.has(current.history_fingerprint)) {
    expanded.add(current.id);
    visited.add(current.history_fingerprint);
    current = byFingerprint.get(current.parent_history_fingerprint);
  }
  return expanded;
}

function countLoadedDescendants(
  node: ConversationBranchNode,
  children: Map<string, ConversationBranchNode[]>
): number {
  const queue = [...(children.get(node.history_fingerprint) || [])];
  const visited = new Set<string>();
  while (queue.length > 0) {
    const current = queue.shift();
    if (!current || visited.has(current.history_fingerprint)) continue;
    visited.add(current.history_fingerprint);
    queue.push(...(children.get(current.history_fingerprint) || []));
  }
  return visited.size;
}

function firstMeaningfulLine(text: string): string {
  return text
    .split("\n")
    .map((line) => line.trim())
    .find(Boolean) || "";
}

function shortFingerprint(fingerprint: string): string {
  if (fingerprint.length <= 14) return fingerprint;
  return `${fingerprint.slice(0, 8)}…${fingerprint.slice(-6)}`;
}
