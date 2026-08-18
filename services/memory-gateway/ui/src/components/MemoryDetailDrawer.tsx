import { useCallback, useEffect, useState, type CSSProperties } from "react";
import {
  ArchiveRestore,
  ArrowUpRight,
  Eye,
  EyeOff,
  GitBranch,
  Pencil,
  Plus,
  Save,
  ShieldAlert,
  Trash2,
  X
} from "lucide-react";
import { MemoryApi } from "../api";
import type {
  DecisionLog,
  MemoryRecord,
  MemorySensitivity,
  MemorySourceExplanation,
  MemorySpace,
  MemoryStability,
  MemoryStatus,
  MemoryType,
  ReviewRecommendation,
  TraversalResponse
} from "../types";
import { badge } from "./Badge";
import { FieldList } from "./FormControls";
import { Modal } from "./Modal";
import { EmptyBlock, ErrorBlock, LoadingBlock } from "./StateBlocks";
import type { ConfirmFn } from "../hooks/useConfirm";
import { useDialogA11y } from "../hooks/useDialogA11y";
import {
  MEMORY_STATUSES,
  MEMORY_TYPES,
  MEMORY_TYPE_COLOR_VAR,
  SENSITIVITIES,
  STABILITIES
} from "../utils/constants";
import { clampNumber, dateText, displayText, errorMessage, percent, reviewActionText, shortId } from "../utils/format";
import {
  contentDivergesFromSource,
  editDraftToPayload,
  editDraftToSpacesPayload,
  memoryToEditDraft,
  normalizeTags,
  spaceNamesFor,
  type MemoryEditDraft
} from "../utils/memory";
import type { Notify } from "../pages/pageTypes";

type DrawerState = {
  loading: boolean;
  error: string | null;
  memory: MemoryRecord | null;
  deleted: boolean;
};

export function MemoryDetailDrawer({
  api,
  memoryId,
  notify,
  confirm,
  expertMode = true,
  onClose,
  onOpenMemory,
  onChanged
}: {
  api: MemoryApi;
  memoryId: string;
  notify: Notify;
  confirm: ConfirmFn;
  expertMode?: boolean;
  onClose: () => void;
  onOpenMemory: (id: string) => void;
  onChanged: () => void;
}) {
  const [state, setState] = useState<DrawerState>({
    loading: true,
    error: null,
    memory: null,
    deleted: false
  });
  const [spaces, setSpaces] = useState<MemorySpace[]>([]);
  const [why, setWhy] = useState<MemorySourceExplanation | null>(null);
  const [traverse, setTraverse] = useState<TraversalResponse | null>(null);
  const [traverseError, setTraverseError] = useState<string | null>(null);
  const [traverseStatus, setTraverseStatus] = useState<"idle" | "loading" | "ready" | "error">("idle");
  const [reviewStatus, setReviewStatus] = useState<"idle" | "loading" | "ready" | "error">("idle");
  const [reviewError, setReviewError] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [editDraft, setEditDraft] = useState<MemoryEditDraft | null>(null);
  const [editError, setEditError] = useState<string | null>(null);
  const [savingEdit, setSavingEdit] = useState(false);
  const [purgeOpen, setPurgeOpen] = useState(false);
  const [purgeConfirmText, setPurgeConfirmText] = useState("");
  const [purging, setPurging] = useState(false);
  const [allFieldsOpen, setAllFieldsOpen] = useState(false);
  const [governance, setGovernance] = useState<{
    review: ReviewRecommendation[];
    logs: DecisionLog[];
  }>({ review: [], logs: [] });

  const drawerRef = useDialogA11y<HTMLElement>(onClose, !purgeOpen);

  const load = useCallback(
    async (signal?: AbortSignal) => {
      setState({ loading: true, error: null, memory: null, deleted: false });
      setWhy(null);
      setTraverse(null);
      setTraverseError(null);
      setTraverseStatus("idle");
      setReviewStatus("idle");
      setReviewError(null);
      setEditing(false);
      setEditDraft(null);
      setEditError(null);
      setAllFieldsOpen(false);
      setGovernance({ review: [], logs: [] });

      // 切换记忆或关闭抽屉后，旧请求的回包不得再写入新档案。
      const alive = () => !signal?.aborted;

      let memory: MemoryRecord | null = null;
      let deleted = false;
      try {
        memory = await api.getMemory(memoryId, { redactSensitive: true }, signal);
      } catch {
        try {
          const trashed = await api.listDeletedMemories({ redactSensitive: true }, signal);
          memory = trashed.find((item) => item.id === memoryId) || null;
          deleted = Boolean(memory);
        } catch {
          memory = null;
        }
      }
      if (!alive()) return;
      if (!memory) {
        setState({ loading: false, error: "没有找到这条记忆，它可能已被永久删除。", memory: null, deleted: false });
        return;
      }
      setState({ loading: false, error: null, memory, deleted });

      void api
        .listMemorySpaces({ signal })
        .then((value) => {
          if (alive()) setSpaces(value);
        })
        .catch(() => {
          if (alive()) setSpaces([]);
        });
      void api
        .decisionLogs(10, { memoryId }, signal)
        .then((logs) => {
          if (alive()) setGovernance((current) => ({ ...current, logs }));
        })
        .catch(() => undefined);
      if (!deleted) {
        void api
          .whyRemember(memoryId, { redactSensitive: true }, signal)
          .then((value) => {
            if (alive()) setWhy(value);
          })
          .catch(() => {
            if (alive()) setWhy(null);
          });
      }
    },
    [api, memoryId]
  );

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const memory = state.memory;

  const revealMemory = async () => {
    if (!memory) return;
    try {
      const full = await api.getMemory(memory.id);
      setState((current) => ({ ...current, memory: full }));
      notify("已显示完整内容", "success");
    } catch (error) {
      notify(errorMessage(error), "error");
    }
  };

  const maskMemory = async () => {
    if (!memory) return;
    try {
      const redacted = await api.getMemory(memory.id, { redactSensitive: true });
      setState((current) => ({ ...current, memory: redacted }));
      notify("已重新遮罩敏感内容", "success");
    } catch (error) {
      notify(errorMessage(error), "error");
    }
  };

  const retryTraverse = async () => {
    if (!memory) return;
    setTraverse(null);
    setTraverseError(null);
    setTraverseStatus("loading");
    try {
      const value = await api.traverseMemoryNetwork(memory.id, { redactSensitive: true });
      setTraverse(value);
      setTraverseStatus("ready");
    } catch (error) {
      setTraverseError(errorMessage(error));
      setTraverseStatus("error");
    }
  };

  const loadReview = async () => {
    if (!memory) return;
    setReviewStatus("loading");
    setReviewError(null);
    try {
      const result = await api.reviewMemories();
      setGovernance((current) => ({
        ...current,
        review: result.recommendations.filter((rec) => rec.memory_ids.includes(memory.id))
      }));
      setReviewStatus("ready");
    } catch (error) {
      setReviewError(errorMessage(error));
      setReviewStatus("error");
    }
  };

  const startEdit = () => {
    if (!memory) return;
    if (memory.redacted) {
      notify("请先显式查看完整内容，再编辑这条记忆。", "info");
      return;
    }
    setEditDraft(memoryToEditDraft(memory, spaces));
    setEditError(null);
    setEditing(true);
  };

  const saveEdit = async () => {
    if (!memory || !editDraft) return;
    if (!editDraft.content.trim()) {
      setEditError("content 不能为空");
      return;
    }
    // 编辑是可撤销的低风险操作，直接保存，不再每次弹确认（danger 级删除仍确认）。
    setSavingEdit(true);
    setEditError(null);
    try {
      const result = await api.updateMemory(
        memory.id,
        editDraftToPayload(editDraft),
        memory.revision
      );
      if (result.archived) {
        notify("记忆已移入回收站", "success");
        setEditing(false);
        setEditDraft(null);
        onChanged();
        onClose();
        return;
      }
      if (!result.memory) {
        throw new Error("更新记忆的响应缺少 memory 字段");
      }
      const spacesResult = await api.updateMemorySpaces(
        memory.id,
        editDraftToSpacesPayload(editDraft),
        result.memory.revision
      );
      const updated = spacesResult.memory || result.memory;
      setState((current) => ({ ...current, memory: updated }));
      notify("记忆已更新", "success");
      setEditing(false);
      setEditDraft(null);
      onChanged();
    } catch (error) {
      setEditError(errorMessage(error));
    } finally {
      setSavingEdit(false);
    }
  };

  const deleteMemory = async () => {
    if (!memory) return;
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
      onChanged();
      onClose();
    } catch (error) {
      notify(errorMessage(error), "error");
    }
  };

  const restoreMemory = async () => {
    if (!memory) return;
    try {
      await api.restoreMemory(memory.id);
      notify("已恢复记忆", "success");
      onChanged();
      await load();
    } catch (error) {
      notify(errorMessage(error), "error");
    }
  };

  const purgeConfirmationCode = memory?.id.slice(0, 8) || "";
  const purgeConfirmed = purgeConfirmText.trim() === purgeConfirmationCode;

  const purgeMemory = async () => {
    if (!memory || !purgeConfirmed) return;
    setPurging(true);
    try {
      await api.purgeDeletedMemory(memory.id);
      notify("已永久删除记忆。", "success");
      setPurgeOpen(false);
      onChanged();
      onClose();
    } catch (error) {
      notify(errorMessage(error), "error");
    } finally {
      setPurging(false);
    }
  };

  const typeColor = memory ? MEMORY_TYPE_COLOR_VAR[memory.type] : "var(--primary)";
  const related = (traverse?.results || []).filter((item) => item.memory.id !== memoryId).slice(0, 8);

  return (
    <div className="memory-profile-layer">
      <button
        className="drawer-scrim memory-profile-scrim"
        type="button"
        aria-label="关闭记忆档案"
        onClick={onClose}
      />
      <aside
        ref={drawerRef}
        className="memory-profile-drawer"
        role="dialog"
        aria-modal="true"
        aria-label="记忆档案"
        tabIndex={-1}
        style={{ "--tc": typeColor } as CSSProperties}
      >
        <header className="profile-topline">
          <span className="profile-eyebrow">
            <i className="profile-type-dot" aria-hidden="true" />
            记忆档案 · {memory ? shortId(memory.id) : shortId(memoryId)}
            {state.deleted && <em className="profile-trash-flag">回收站</em>}
          </span>
          <button className="icon-button" type="button" onClick={onClose} title="关闭" aria-label="关闭">
            <X size={18} />
          </button>
        </header>

        {state.loading && <LoadingBlock label="正在打开记忆档案" />}
        {state.error && <ErrorBlock message={state.error} onRetry={() => void load()} />}

        {memory && !editing && (
          <>
            <div className="profile-badges">
              {badge(memory.type)}
              {badge(memory.status || "dynamic")}
              {badge(memory.stability)}
              {memory.sensitivity !== "normal" && badge(memory.sensitivity)}
            </div>

            {memory.redacted && (
              <div className="notice warning">
                <ShieldAlert size={16} />
                正文和来源原文已遮罩，真实数据未被改写。
                {!state.deleted && (
                  <button className="link-inline" type="button" onClick={() => void revealMemory()}>
                    <Eye size={14} />
                    查看完整内容
                  </button>
                )}
              </div>
            )}

            {!memory.redacted && memory.sensitivity !== "normal" && (
              <div className="notice">
                <ShieldAlert size={16} />
                正在显示完整敏感内容。
                <button className="link-inline" type="button" onClick={() => void maskMemory()}>
                  <EyeOff size={14} />
                  重新遮罩
                </button>
              </div>
            )}

            <blockquote className="profile-content">{memory.content}</blockquote>

            {(memory.topics?.length || memory.entities?.length || memory.space_ids?.length) ? (
              <div className="profile-chips">
                {(memory.topics || []).map((topic) => (
                  <span className="profile-chip" key={`t-${topic}`}>#{topic}</span>
                ))}
                {(memory.entities || []).map((entity) => (
                  <span className="profile-chip entity" key={`e-${entity}`}>@{entity}</span>
                ))}
                {spaceNamesFor(memory, spaces).map((name) => (
                  <span className="profile-chip space" key={`s-${name}`}>{name}</span>
                ))}
              </div>
            ) : null}

            <div className="profile-meta-grid">
              <div>
                <span>重要度</span>
                <strong>{memory.importance}<small> / 10</small></strong>
              </div>
              <div>
                <span>置信度</span>
                <strong>{percent(memory.confidence)}</strong>
              </div>
              <div>
                <span>正向度</span>
                <strong>{percent(memory.valence)}</strong>
              </div>
              <div>
                <span>唤起度</span>
                <strong>{percent(memory.arousal)}</strong>
              </div>
              <div>
                <span>激活次数</span>
                <strong>{memory.usage_count}</strong>
              </div>
              <div>
                <span>最近使用</span>
                <strong>{dateText(memory.last_used_at) || "-"}</strong>
              </div>
              <div>
                <span>{expertMode ? "向量空间" : "语义检索"}</span>
                <strong>
                  {expertMode
                    ? memory.embedding_space_id || "无向量"
                    : memory.embedding_space_id
                      ? "已启用"
                      : "未启用"}
                </strong>
              </div>
            </div>

            <div className="drawer-actions profile-actions">
              {!state.deleted && (
                <>
                  <button
                    className="secondary-button"
                    type="button"
                    disabled={Boolean(memory.redacted)}
                    onClick={startEdit}
                  >
                    <Pencil size={16} />
                    编辑
                  </button>
                  <button className="danger-button" type="button" onClick={() => void deleteMemory()}>
                    <Trash2 size={16} />
                    移入回收站
                  </button>
                </>
              )}
              {state.deleted && (
                <>
                  <button className="primary-button" type="button" onClick={() => void restoreMemory()}>
                    <ArchiveRestore size={16} />
                    恢复
                  </button>
                  <button
                    className="danger-button"
                    type="button"
                    onClick={() => {
                      setPurgeConfirmText("");
                      setPurgeOpen(true);
                    }}
                  >
                    <Trash2 size={16} />
                    永久删除
                  </button>
                </>
              )}
            </div>

            {(memory.source_message || why) && (
              <section className="subpanel profile-source">
                <h3>来源</h3>
                {memory.source_message &&
                  contentDivergesFromSource(memory.content, memory.source_message) && (
                    <div className="notice warning">
                      <ShieldAlert size={16} />
                      正文与原始来源已明显不同（可能经过编辑），以下原文仅供追溯。
                    </div>
                  )}
                {memory.source_message && <blockquote>{memory.source_message}</blockquote>}
                {why && (
                  <FieldList
                    compact
                    entries={[
                      ["来源摘录", why.source_excerpt],
                      ["来源对话 ID", why.source_conversation_id],
                      ["保存时间", dateText(why.saved_at)],
                      ["是否核心记忆证据", why.is_core_memory_evidence ? "是" : "否"],
                      ["核心记忆分区", why.core_memory_sections.map(displayText)]
                    ]}
                  />
                )}
              </section>
            )}

            <TemporalFacts memory={memory} />

            {!state.deleted && (
              <section className="subpanel profile-related">
                <h3>
                  <GitBranch size={15} />
                  关联记忆
                  {traverse && <small>{related.length ? `按图关系强度排序` : ""}</small>}
                </h3>
                {traverseStatus === "idle" && (
                  <button className="secondary-button compact" type="button" onClick={() => void retryTraverse()}>
                    加载关联分析（实验）
                  </button>
                )}
                {traverseStatus === "loading" && <LoadingBlock label="正在遍历记忆网络" />}
                {traverseStatus === "error" && traverseError && (
                  <ErrorBlock message={`关联记忆加载失败：${traverseError}`} onRetry={() => void retryTraverse()} />
                )}
                {traverseStatus === "ready" && traverse && related.length === 0 && (
                  <EmptyBlock compact label="这条记忆暂时没有足够强的关联。" />
                )}
                {related.map((item) => (
                  <button
                    key={item.memory.id}
                    type="button"
                    className="profile-related-item"
                    style={{ "--tc": MEMORY_TYPE_COLOR_VAR[item.memory.type] } as CSSProperties}
                    onClick={() => onOpenMemory(item.memory.id)}
                  >
                    <span className="related-type">{displayText(item.memory.type)}</span>
                    <strong>{item.memory.content}</strong>
                    <span className="related-meta">
                      关联强度 {percent(item.score)} · 距离 {item.depth}
                      <ArrowUpRight size={13} />
                    </span>
                  </button>
                ))}
              </section>
            )}

            {(!state.deleted || governance.logs.length > 0) && (
              <section className="subpanel profile-governance">
                <h3>治理记录</h3>
                {!state.deleted && reviewStatus === "idle" && (
                  <button className="secondary-button compact" type="button" onClick={() => void loadReview()}>
                    加载治理建议
                  </button>
                )}
                {!state.deleted && reviewStatus === "loading" && <LoadingBlock label="正在运行记忆体检" />}
                {!state.deleted && reviewStatus === "error" && reviewError && (
                  <ErrorBlock message={`治理建议加载失败：${reviewError}`} onRetry={() => void loadReview()} />
                )}
                {!state.deleted && reviewStatus === "ready" && governance.review.length === 0 && (
                  <EmptyBlock compact label="这条记忆目前没有治理建议。" />
                )}
                {governance.review.map((rec, index) => (
                  <div className="profile-review-item" key={`review-${index}`}>
                    <span className={`severity-pill severity-${rec.severity}`}>
                      {displayText(rec.severity)}
                    </span>
                    <div>
                      <strong>{reviewActionText(rec.action)}</strong>
                      <p>{rec.reason}</p>
                    </div>
                  </div>
                ))}
                {governance.logs.map((log) => (
                  <div className="profile-log-item" key={log.id}>
                    {badge(log.decision)}
                    <div>
                      <p>{log.reason}</p>
                      <small>{dateText(log.created_at)}</small>
                    </div>
                  </div>
                ))}
              </section>
            )}

            {expertMode && (
              <section className="subpanel profile-all-fields">
                <button
                  className="ghost-button compact"
                  type="button"
                  aria-expanded={allFieldsOpen}
                  onClick={() => setAllFieldsOpen((current) => !current)}
                >
                  {allFieldsOpen ? "收起全部字段" : "查看全部字段"}
                </button>
                {allFieldsOpen && (
                  <FieldList
                    compact
                    entries={[
                      ["id", memory.id],
                      ["类型", displayText(memory.type)],
                      ["状态", displayText(memory.status || "dynamic")],
                      ["敏感级别", displayText(memory.sensitivity)],
                      ["稳定性", displayText(memory.stability)],
                      ["已消化", memory.digested ? "是" : "否"],
                      ["衰减 λ", memory.decay_lambda ?? "-"],
                      ["复核时间", memory.review_after],
                      ["证据记忆 ID", memory.evidence_memory_ids],
                      ["来源对话 ID", memory.source_conversation_id],
                      ["创建时间", memory.created_at],
                      ["更新时间", memory.updated_at]
                    ]}
                  />
                )}
              </section>
            )}
          </>
        )}

        {memory && editing && editDraft && (
          <div className="edit-form">
            <label className="field-block">
              <span>内容</span>
              <textarea
                value={editDraft.content}
                rows={5}
                onChange={(event) => setEditDraft({ ...editDraft, content: event.target.value })}
              />
            </label>
            {editDraft.content.trim() !== memory.content && (
              <div className="notice warning">
                修改正文后，这条记忆的语义索引会过期，语义搜索可能暂时找不到它；重新写入或等待后台重建后恢复。
              </div>
            )}
            <div className="classification-edit-grid">
              <TagEditor
                label="主题"
                values={editDraft.topics}
                placeholder="添加主题"
                onChange={(topics) => setEditDraft({ ...editDraft, topics })}
              />
              <TagEditor
                label="实体"
                values={editDraft.entities}
                placeholder="添加人物、项目或地点"
                onChange={(entities) => setEditDraft({ ...editDraft, entities })}
              />
              <TagEditor
                label="空间"
                values={editDraft.space_names}
                placeholder="添加空间"
                onChange={(space_names) => setEditDraft({ ...editDraft, space_names })}
              />
            </div>
            <div className="edit-grid">
              <label className="field-block">
                <span>类型</span>
                <select
                  value={editDraft.type}
                  onChange={(event) => setEditDraft({ ...editDraft, type: event.target.value as MemoryType })}
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
              <label className="field-block range-slider">
                <span>情绪正向度 {percent(editDraft.valence)}</span>
                <input
                  type="range"
                  min={0}
                  max={1}
                  step={0.05}
                  value={editDraft.valence}
                  onChange={(event) =>
                    setEditDraft({
                      ...editDraft,
                      valence: clampNumber(Number(event.target.value), 0, 1)
                    })
                  }
                />
              </label>
              <label className="field-block range-slider">
                <span>唤起度 {percent(editDraft.arousal)}</span>
                <input
                  type="range"
                  min={0}
                  max={1}
                  step={0.05}
                  value={editDraft.arousal}
                  onChange={(event) =>
                    setEditDraft({
                      ...editDraft,
                      arousal: clampNumber(Number(event.target.value), 0, 1)
                    })
                  }
                />
              </label>
              <label className="field-block">
                <span>稳定性</span>
                <select
                  value={editDraft.stability}
                  onChange={(event) =>
                    setEditDraft({ ...editDraft, stability: event.target.value as MemoryStability })
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
                <span>状态</span>
                <select
                  value={editDraft.status}
                  onChange={(event) =>
                    setEditDraft({ ...editDraft, status: event.target.value as MemoryStatus })
                  }
                >
                  {MEMORY_STATUSES.map((status) => (
                    <option key={status} value={status}>
                      {displayText(status)}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field-block">
                <span>敏感级别</span>
                <select
                  value={editDraft.sensitivity}
                  onChange={(event) =>
                    setEditDraft({ ...editDraft, sensitivity: event.target.value as MemorySensitivity })
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
                  placeholder="YYYY-MM-DD（ISO 8601）"
                  onChange={(event) => setEditDraft({ ...editDraft, valid_until: event.target.value })}
                />
              </label>
              <label className="field-block">
                <span>复核时间</span>
                <input
                  value={editDraft.review_after}
                  placeholder="YYYY-MM-DD（ISO 8601）"
                  onChange={(event) => setEditDraft({ ...editDraft, review_after: event.target.value })}
                />
              </label>
              <label className="field-block">
                <span>来源对话 ID</span>
                <input
                  value={editDraft.source_conversation_id}
                  onChange={(event) =>
                    setEditDraft({ ...editDraft, source_conversation_id: event.target.value })
                  }
                />
              </label>
            </div>
            <label className="field-block">
              <span>来源原文</span>
              <textarea
                value={editDraft.source_message}
                rows={3}
                onChange={(event) => setEditDraft({ ...editDraft, source_message: event.target.value })}
              />
            </label>
            {editError && (
              <div className="notice warning">
                <ShieldAlert size={16} />
                {editError}
              </div>
            )}
            <div className="drawer-actions">
              <button className="primary-button" type="button" disabled={savingEdit} onClick={() => void saveEdit()}>
                <Save size={16} />
                保存
              </button>
              <button
                className="ghost-button"
                type="button"
                disabled={savingEdit}
                onClick={() => {
                  setEditing(false);
                  setEditDraft(null);
                  setEditError(null);
                }}
              >
                <X size={16} />
                取消
              </button>
            </div>
          </div>
        )}
      </aside>

      {purgeOpen && memory && (
        <Modal
          title="永久删除记忆"
          className="confirm-card confirm-danger"
          closeDisabled={purging}
          onClose={() => {
            if (!purging) setPurgeOpen(false);
          }}
        >
          <div className="confirm-body purge-confirm-body">
            <div className="notice warning">
              <ShieldAlert size={16} />
              永久删除后无法从回收站恢复，审计日志只会保留删除摘要。
            </div>
            <FieldList
              entries={[
                ["完整 ID", memory.id],
                ["确认码", purgeConfirmationCode],
                ["内容预览", memory.content]
              ]}
            />
            <label className="field-block">
              <span>输入 8 位确认码</span>
              <input
                autoFocus
                value={purgeConfirmText}
                disabled={purging}
                onChange={(event) => setPurgeConfirmText(event.target.value)}
              />
            </label>
          </div>
          <div className="drawer-actions end">
            <button className="ghost-button" type="button" disabled={purging} onClick={() => setPurgeOpen(false)}>
              取消
            </button>
            <button
              className="danger-button"
              type="button"
              disabled={!purgeConfirmed || purging}
              onClick={() => void purgeMemory()}
            >
              <Trash2 size={16} />
              永久删除
            </button>
          </div>
        </Modal>
      )}
    </div>
  );
}

function TemporalFacts({ memory }: { memory: MemoryRecord }) {
  const hasDetails = Boolean(
    memory.valid_from ||
      memory.valid_until ||
      memory.temporal_subject ||
      memory.temporal_predicate ||
      memory.supersedes ||
      memory.superseded_by
  );
  const isHistorical = (memory.status || "dynamic") === "resolved" && Boolean(memory.superseded_by);
  if (!hasDetails && !isHistorical) return null;
  return (
    <section className="subpanel temporal-fact-panel">
      <h3>时间事实</h3>
      {isHistorical && (
        <div className="notice warning temporal-fact-notice">
          <ShieldAlert size={16} />
          历史事实，已被新事实取代。
        </div>
      )}
      <FieldList
        compact
        entries={[
          ["生效时间", memory.valid_from],
          ["有效期至", memory.valid_until],
          ["时间主体", memory.temporal_subject],
          ["时间谓词", memory.temporal_predicate],
          ["取代的记忆 ID", memory.supersedes],
          ["被取代为记忆 ID", memory.superseded_by]
        ]}
      />
    </section>
  );
}

function TagEditor({
  label,
  values,
  placeholder,
  onChange
}: {
  label: string;
  values: string[];
  placeholder: string;
  onChange: (values: string[]) => void;
}) {
  const [draft, setDraft] = useState("");
  const addTag = () => {
    const next = normalizeTags([...values, draft]);
    if (next.length !== values.length || next.some((value, index) => value !== values[index])) {
      onChange(next);
    }
    setDraft("");
  };

  return (
    <div className="tag-editor">
      <span>{label}</span>
      <div className="tag-list">
        {values.length === 0 && <small>未设置</small>}
        {values.map((value) => (
          <button
            key={value}
            className="tag-chip"
            type="button"
            onClick={() => onChange(values.filter((item) => item !== value))}
            title={`移除 ${value}`}
          >
            {value}
            <X size={13} />
          </button>
        ))}
      </div>
      <div className="tag-input-row">
        <input
          value={draft}
          placeholder={placeholder}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              addTag();
            }
          }}
        />
        <button className="icon-button" type="button" onClick={addTag} title="添加">
          <Plus size={15} />
        </button>
      </div>
    </div>
  );
}
