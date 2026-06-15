import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Activity,
  ArchiveRestore,
  Clipboard,
  Download,
  Eye,
  EyeOff,
  FileText,
  KeyRound,
  Layers3,
  ListChecks,
  Pencil,
  RefreshCcw,
  Save,
  Search,
  ShieldAlert,
  Trash2,
  Upload,
  Wrench,
  X
} from "lucide-react";
import { MemoryApi } from "../../api";
import { normalizeBaseUrl } from "../../storage";
import type {
  ConnectionSettings,
  CoreMemoryHistoryItem,
  CoreMemorySection,
  CoreSectionName,
  DecisionLog,
  MemoryAction,
  MemoryExport,
  MemoryRecord,
  MemoryReport,
  MemorySourceExplanation,
  MemoryStability,
  MemorySensitivity,
  MemoryType,
  PageKey,
  RecentContextSummary,
  RestoreResult,
  ReviewAction,
  ReviewRecommendation,
  ReviewResult
} from "../../types";
import { badge } from "../../components/Badge";
import { FieldList, FilterSelect, RangeFields } from "../../components/FormControls";
import { PageHeader } from "../../components/PageHeader";
import { InfoCard, StatCard } from "../../components/StatCard";
import { EmptyBlock, ErrorBlock, LoadingBlock } from "../../components/StateBlocks";
import type { ConfirmFn } from "../../hooks/useConfirm";
import type { LoadState } from "../../hooks/useAsyncData";
import {
  CONFIG_KEYS,
  CORE_SECTIONS,
  DECISIONS,
  MEMORY_TYPES,
  REVIEW_ACTIONS,
  SENSITIVITIES,
  STABILITIES
} from "../../utils/constants";
import { downloadFile, copyText } from "../../utils/files";
import {
  candidateSummary,
  clampNumber,
  dateText,
  displayText,
  errorMessage,
  joinUrl,
  maskSecret,
  percent,
  prettyJson,
  reportSectionTitle,
  reviewActionText,
  sectionTitle,
  shortId
} from "../../utils/format";
import { editDraftToPayload, memoryToEditDraft } from "../../utils/memory";
import type { MemoryEditDraft, MemoryFilters } from "../../utils/memory";
import type { Notify } from "../pageTypes";

export function MemoriesPage({
  api,
  notify,
  confirm
}: {
  api: MemoryApi;
  notify: Notify;
  confirm: ConfirmFn;
}) {
  const [tab, setTab] = useState<"active" | "deleted">("active");
  const [state, setState] = useState<LoadState<MemoryRecord[]>>({
    loading: true,
    error: null,
    data: null
  });
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<MemoryRecord | null>(null);
  const [editing, setEditing] = useState(false);
  const [editDraft, setEditDraft] = useState<MemoryEditDraft | null>(null);
  const [editError, setEditError] = useState<string | null>(null);
  const [savingEdit, setSavingEdit] = useState(false);
  const [why, setWhy] = useState<LoadState<MemorySourceExplanation>>({
    loading: false,
    error: null,
    data: null
  });
  const [filters, setFilters] = useState<MemoryFilters>({
    type: "all",
    sensitivity: "all",
    stability: "all",
    minImportance: 1,
    maxImportance: 10,
    minConfidence: 0,
    maxConfidence: 1,
    hasValidUntil: false,
    hasReviewAfter: false
  });

  const load = useCallback(
    async (mode = tab) => {
      setState({ loading: true, error: null, data: null });
      setWhy({ loading: false, error: null, data: null });
      try {
        const memories =
          mode === "deleted"
            ? await api.listDeletedMemories()
            : query.trim()
              ? await api.searchMemories(query.trim(), 20)
              : await api.listMemories();
        setState({ loading: false, error: null, data: memories });
      } catch (error) {
        setState({ loading: false, error: errorMessage(error), data: null });
      }
    },
    [api, query, tab]
  );

  useEffect(() => {
    void load(tab);
  }, [load, tab]);

  useEffect(() => {
    setWhy({ loading: false, error: null, data: null });
    setEditing(false);
    setEditDraft(null);
    setEditError(null);
    setSavingEdit(false);
  }, [selected?.id]);

  const memories = useMemo(() => {
    return (state.data || []).filter((memory) => {
      if (filters.type !== "all" && memory.type !== filters.type) return false;
      if (filters.sensitivity !== "all" && memory.sensitivity !== filters.sensitivity) return false;
      if (filters.stability !== "all" && memory.stability !== filters.stability) return false;
      if (memory.importance < filters.minImportance || memory.importance > filters.maxImportance) return false;
      if (memory.confidence < filters.minConfidence || memory.confidence > filters.maxConfidence) return false;
      if (filters.hasValidUntil && !memory.valid_until) return false;
      if (filters.hasReviewAfter && !memory.review_after) return false;
      return true;
    });
  }, [filters, state.data]);

  const runWhy = async (memoryId: string) => {
    setWhy({ loading: true, error: null, data: null });
    try {
      setWhy({ loading: false, error: null, data: await api.whyRemember(memoryId) });
    } catch (error) {
      setWhy({ loading: false, error: errorMessage(error), data: null });
    }
  };

  const deleteMemory = async (memory: MemoryRecord) => {
    if (
      !(await confirm({
        title: "移入回收站",
        message: `确认将这条记忆移入回收站？\n\n${memory.content}`,
        tone: "danger",
        confirmLabel: "移入回收站"
      }))
    ) {
      return;
    }
    try {
      await api.deleteMemory(memory.id);
      notify("已移入回收站，可在回收站恢复。", "success");
      setSelected(null);
      await load("active");
    } catch (error) {
      notify(errorMessage(error), "error");
    }
  };

  const restoreMemory = async (memory: MemoryRecord) => {
    try {
      await api.restoreMemory(memory.id);
      notify("已恢复记忆", "success");
      setSelected(null);
      await load("deleted");
    } catch (error) {
      notify(errorMessage(error), "error");
    }
  };

  const startEdit = () => {
    if (!selected) return;
    setEditDraft(memoryToEditDraft(selected));
    setEditError(null);
    setEditing(true);
  };

  const cancelEdit = () => {
    setEditing(false);
    setEditDraft(null);
    setEditError(null);
  };

  const saveEdit = async () => {
    if (!selected || !editDraft) return;
    if (!editDraft.content.trim()) {
      setEditError("content 不能为空");
      return;
    }
    if (
      !(await confirm({
        title: "更新记忆",
        message: "确定要更新这条记忆吗？这会影响后续检索和回答注入。",
        tone: "warning",
        confirmLabel: "更新"
      }))
    ) {
      return;
    }
    setSavingEdit(true);
    setEditError(null);
    try {
      const result = await api.updateMemory(selected.id, editDraftToPayload(editDraft));
      setSelected(result.memory);
      setState((current) =>
        current.data
          ? {
              ...current,
              data: current.data.map((memory) =>
                memory.id === result.memory.id ? result.memory : memory
              )
            }
          : current
      );
      notify("记忆已更新", "success");
      setEditing(false);
      setEditDraft(null);
      await load("active");
    } catch (error) {
      setEditError(errorMessage(error));
    } finally {
      setSavingEdit(false);
    }
  };

  return (
    <div className="page-stack">
      <PageHeader
        title="记忆库"
        subtitle="查看、搜索、筛选、解释、删除和恢复记忆。"
        action={
          <div className="button-row">
            <button className="secondary-button" type="button" onClick={() => load(tab)}>
              <RefreshCcw size={16} />
              刷新
            </button>
          </div>
        }
      />

      <div className="tabs">
        <button
          className={tab === "active" ? "active" : ""}
          type="button"
          onClick={() => {
            setTab("active");
            setSelected(null);
          }}
        >
          活跃记忆
        </button>
        <button
          className={tab === "deleted" ? "active" : ""}
          type="button"
          onClick={() => {
            setTab("deleted");
            setSelected(null);
          }}
        >
          回收站
        </button>
      </div>

      <div className={`memory-layout ${selected ? "has-detail" : ""}`}>
        <aside className="filter-panel">
          <div className="search-box">
            <Search size={16} />
            <input
              value={query}
              disabled={tab === "deleted"}
              placeholder="搜索记忆"
              onChange={(event) => setQuery(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  void load("active");
                }
              }}
            />
          </div>
          <button
            className="primary-button full-width"
            type="button"
            disabled={tab === "deleted" || !query.trim()}
            onClick={() => load("active")}
          >
            搜索
          </button>
          <button
            className="ghost-button full-width"
            type="button"
            onClick={() => {
              setQuery("");
              void load(tab);
            }}
          >
            清空搜索
          </button>

          <FilterSelect
            label="类型"
            value={filters.type}
            options={["all", ...MEMORY_TYPES]}
            onChange={(value) => setFilters({ ...filters, type: value as MemoryFilters["type"] })}
          />
          <FilterSelect
            label="敏感级别"
            value={filters.sensitivity}
            options={["all", ...SENSITIVITIES]}
            onChange={(value) =>
              setFilters({ ...filters, sensitivity: value as MemoryFilters["sensitivity"] })
            }
          />
          <FilterSelect
            label="稳定性"
            value={filters.stability}
            options={["all", ...STABILITIES]}
            onChange={(value) =>
              setFilters({ ...filters, stability: value as MemoryFilters["stability"] })
            }
          />
          <RangeFields
            label="重要度"
            min={1}
            max={10}
            step={1}
            from={filters.minImportance}
            to={filters.maxImportance}
            onChange={(from, to) =>
              setFilters({ ...filters, minImportance: from, maxImportance: to })
            }
          />
          <RangeFields
            label="置信度"
            min={0}
            max={1}
            step={0.05}
            from={filters.minConfidence}
            to={filters.maxConfidence}
            onChange={(from, to) =>
              setFilters({ ...filters, minConfidence: from, maxConfidence: to })
            }
          />
          <label className="checkbox-row">
            <input
              type="checkbox"
              checked={filters.hasValidUntil}
              onChange={(event) => setFilters({ ...filters, hasValidUntil: event.target.checked })}
            />
            有有效期
          </label>
          <label className="checkbox-row">
            <input
              type="checkbox"
              checked={filters.hasReviewAfter}
              onChange={(event) => setFilters({ ...filters, hasReviewAfter: event.target.checked })}
            />
            有复核时间
          </label>
        </aside>

        <section className="panel memory-table-panel">
          {state.loading && <LoadingBlock label="正在加载记忆" />}
          {state.error && <ErrorBlock message={state.error} onRetry={() => load(tab)} />}
          {!state.loading && !state.error && memories.length === 0 && (
            <EmptyBlock label={tab === "deleted" ? "回收站为空" : "没有匹配的记忆"} />
          )}
          {memories.length > 0 && (
            <div className="table-wrap">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>内容</th>
                    <th>类型</th>
                    <th>重要度</th>
                    <th>置信度</th>
                    <th>稳定性</th>
                    <th>敏感级别</th>
                    <th>使用次数</th>
                    <th>最近使用</th>
                    <th>更新时间</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {memories.map((memory) => (
                    <tr
                      key={memory.id}
                      className={selected?.id === memory.id ? "selected" : ""}
                      onClick={() => setSelected(memory)}
                    >
                      <td className="content-cell">{memory.content}</td>
                      <td>{badge(memory.type)}</td>
                      <td>{memory.importance}</td>
                      <td>{percent(memory.confidence)}</td>
                      <td>{badge(memory.stability)}</td>
                      <td>{badge(memory.sensitivity)}</td>
                      <td>{memory.usage_count}</td>
                      <td>{dateText(memory.last_used_at)}</td>
                      <td>{dateText(memory.updated_at)}</td>
                      <td>
                        {tab === "deleted" ? (
                          <button
                            className="secondary-button compact"
                            type="button"
                            onClick={(event) => {
                              event.stopPropagation();
                              void restoreMemory(memory);
                            }}
                          >
                            恢复
                          </button>
                        ) : (
                          <button
                            className="danger-button compact"
                            type="button"
                            onClick={(event) => {
                              event.stopPropagation();
                              void deleteMemory(memory);
                            }}
                          >
                            移入回收站
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>

        {selected && (
          <aside className="detail-drawer">
            <div className="drawer-header">
              <h2>记忆详情</h2>
              <button className="icon-button" type="button" onClick={() => setSelected(null)} title="关闭">
                <X size={18} />
              </button>
            </div>
            {editing && editDraft ? (
              <div className="edit-form">
                <label className="field-block">
                  <span>内容</span>
                  <textarea
                    value={editDraft.content}
                    rows={5}
                    onChange={(event) =>
                      setEditDraft({ ...editDraft, content: event.target.value })
                    }
                  />
                </label>
                {editDraft.content.trim() !== selected.content && (
                  <div className="notice warning">
                    修改内容后，旧 embedding 会失效；后续版本可提供重建 embedding。
                  </div>
                )}
                <div className="edit-grid">
                  <label className="field-block">
                    <span>类型</span>
                    <select
                      value={editDraft.type}
                      onChange={(event) =>
                        setEditDraft({ ...editDraft, type: event.target.value as MemoryType })
                      }
                    >
                      {MEMORY_TYPES.map((type) => (
                        <option key={type} value={type}>
                          {displayText(type)}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="field-block">
                    <span>重要度</span>
                    <input
                      type="number"
                      min={1}
                      max={10}
                      step={1}
                      value={editDraft.importance}
                      onChange={(event) =>
                        setEditDraft({
                          ...editDraft,
                          importance: Math.round(clampNumber(Number(event.target.value), 1, 10))
                        })
                      }
                    />
                  </label>
                  <label className="field-block">
                    <span>置信度</span>
                    <input
                      type="number"
                      min={0}
                      max={1}
                      step={0.05}
                      value={editDraft.confidence}
                      onChange={(event) =>
                        setEditDraft({
                          ...editDraft,
                          confidence: clampNumber(Number(event.target.value), 0, 1)
                        })
                      }
                    />
                  </label>
                  <label className="field-block">
                    <span>稳定性</span>
                    <select
                      value={editDraft.stability}
                      onChange={(event) =>
                        setEditDraft({
                          ...editDraft,
                          stability: event.target.value as MemoryStability
                        })
                      }
                    >
                      {STABILITIES.map((stability) => (
                        <option key={stability} value={stability}>
                          {displayText(stability)}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="field-block">
                    <span>敏感级别</span>
                    <select
                      value={editDraft.sensitivity}
                      onChange={(event) =>
                        setEditDraft({
                          ...editDraft,
                          sensitivity: event.target.value as MemorySensitivity
                        })
                      }
                    >
                      {SENSITIVITIES.map((sensitivity) => (
                        <option key={sensitivity} value={sensitivity}>
                          {displayText(sensitivity)}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="field-block">
                    <span>有效期</span>
                    <input
                      value={editDraft.valid_until}
                      onChange={(event) =>
                        setEditDraft({ ...editDraft, valid_until: event.target.value })
                      }
                    />
                  </label>
                  <label className="field-block">
                    <span>复核时间</span>
                    <input
                      value={editDraft.review_after}
                      onChange={(event) =>
                        setEditDraft({ ...editDraft, review_after: event.target.value })
                      }
                    />
                  </label>
                  <label className="field-block">
                    <span>来源对话 ID</span>
                    <input
                      value={editDraft.source_conversation_id}
                      onChange={(event) =>
                        setEditDraft({
                          ...editDraft,
                          source_conversation_id: event.target.value
                        })
                      }
                    />
                  </label>
                </div>
                <label className="field-block">
                  <span>来源原文</span>
                  <textarea
                    value={editDraft.source_message}
                    rows={3}
                    onChange={(event) =>
                      setEditDraft({ ...editDraft, source_message: event.target.value })
                    }
                  />
                </label>
                {editError && (
                  <div className="notice warning">
                    <ShieldAlert size={16} />
                    {editError}
                  </div>
                )}
              </div>
            ) : (
              <FieldList
                entries={[
                  ["id", selected.id],
                  ["内容", selected.content],
                  ["类型", displayText(selected.type)],
                  ["重要度", selected.importance],
                  ["置信度", percent(selected.confidence)],
                  ["来源原文", selected.source_message],
                  ["来源对话 ID", selected.source_conversation_id],
                  ["使用次数", selected.usage_count],
                  ["最近使用", selected.last_used_at],
                  ["稳定性", displayText(selected.stability)],
                  ["有效期", selected.valid_until],
                  ["复核时间", selected.review_after],
                  ["敏感级别", displayText(selected.sensitivity)],
                  ["证据记忆 ID", selected.evidence_memory_ids],
                  ["创建时间", selected.created_at],
                  ["更新时间", selected.updated_at]
                ]}
              />
            )}
            <div className="drawer-actions">
              {tab === "active" && editing && (
                <>
                  <button
                    className="primary-button"
                    type="button"
                    disabled={savingEdit}
                    onClick={saveEdit}
                  >
                    <Save size={16} />
                    保存
                  </button>
                  <button
                    className="ghost-button"
                    type="button"
                    disabled={savingEdit}
                    onClick={cancelEdit}
                  >
                    <X size={16} />
                    取消
                  </button>
                </>
              )}
              {tab === "active" && !editing && (
                <>
                  <button className="secondary-button" type="button" onClick={startEdit}>
                    <Pencil size={16} />
                    编辑
                  </button>
                  <button
                    className="secondary-button"
                    type="button"
                    onClick={() => runWhy(selected.id)}
                  >
                    为什么记得？
                  </button>
                  <button
                    className="danger-button"
                    type="button"
                    onClick={() => deleteMemory(selected)}
                  >
                    <Trash2 size={16} />
                    移入回收站
                  </button>
                </>
              )}
              {tab === "deleted" && (
                <button
                  className="primary-button"
                  type="button"
                  onClick={() => restoreMemory(selected)}
                >
                  <ArchiveRestore size={16} />
                  恢复
                </button>
              )}
            </div>
            {!editing && why.loading && <LoadingBlock label="正在读取来源" />}
            {!editing && why.error && <ErrorBlock message={why.error} />}
            {!editing && why.data && (
              <section className="subpanel">
                <h3>为什么记得？</h3>
                <FieldList
                  entries={[
                    ["来源摘录", why.data.source_excerpt],
                    ["来源对话 ID", why.data.source_conversation_id],
                    ["保存时间", why.data.saved_at],
                    ["更新时间", why.data.updated_at],
                    ["置信度", percent(why.data.confidence)],
                    ["是否核心记忆证据", why.data.is_core_memory_evidence ? "是" : "否"],
                    ["核心记忆分区", why.data.core_memory_sections.map(displayText)],
                    ["证据记忆 ID", why.data.evidence_memory_ids]
                  ]}
                />
              </section>
            )}
          </aside>
        )}
      </div>
    </div>
  );
}



