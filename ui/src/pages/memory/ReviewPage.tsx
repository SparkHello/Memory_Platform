import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Activity,
  ArrowDown,
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
import { Modal } from "../../components/Modal";
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

export function ReviewPage({
  api,
  notify,
  confirm
}: {
  api: MemoryApi;
  notify: Notify;
  confirm: ConfirmFn;
}) {
  const [state, setState] = useState<
    LoadState<{ review: ReviewResult; memories: MemoryRecord[] }>
  >({ loading: true, error: null, data: null });
  const [mergeDraft, setMergeDraft] = useState<ReviewRecommendation | null>(null);
  const [mergeContent, setMergeContent] = useState("");
  const [applying, setApplying] = useState(false);

  const load = useCallback(async (showToast = false) => {
    setState({ loading: true, error: null, data: null });
    try {
      const [review, memories] = await Promise.all([
        api.reviewMemories(),
        api.listMemories()
      ]);
      setState({ loading: false, error: null, data: { review, memories } });
      if (showToast) {
        notify(`体检完成，共 ${review.recommendations.length} 条建议`, "success");
      }
    } catch (error) {
      setState({ loading: false, error: errorMessage(error), data: null });
      if (showToast) {
        notify(errorMessage(error), "error");
      }
    }
  }, [api, notify]);

  useEffect(() => {
    void load();
  }, [load]);

  const memoryMap = useMemo(() => {
    return new Map((state.data?.memories || []).map((memory) => [memory.id, memory]));
  }, [state.data?.memories]);

  const grouped = useMemo(() => {
    const map = new Map<ReviewAction, ReviewRecommendation[]>(
      REVIEW_ACTIONS.map((action) => [action, []])
    );
    for (const recommendation of state.data?.review.recommendations || []) {
      map.get(recommendation.action)?.push(recommendation);
    }
    return map;
  }, [state.data?.review.recommendations]);

  const applyDelete = async (recommendation: ReviewRecommendation) => {
    if (
      !(await confirm({
        title: "移入回收站",
        message: `确认将 ${recommendation.memory_ids.length} 条建议删除的记忆移入回收站？`,
        tone: "danger",
        confirmLabel: "移入回收站"
      }))
    ) {
      return;
    }
    setApplying(true);
    try {
      for (const memoryId of recommendation.memory_ids) {
        await api.deleteMemory(memoryId);
      }
      notify("已移入回收站，可在回收站恢复。", "success");
      await load();
    } catch (error) {
      notify(errorMessage(error), "error");
    } finally {
      setApplying(false);
    }
  };

  const applyMerge = async () => {
    if (!mergeDraft) return;
    if (
      !(await confirm({
        title: "合并记忆",
        message: "确认合并这些记忆？合并后多余记忆会进入回收站。",
        tone: "warning",
        confirmLabel: "合并"
      }))
    ) {
      return;
    }
    setApplying(true);
    try {
      await api.mergeMemories(mergeDraft.memory_ids, mergeContent.trim() || mergeDraft.suggested_content);
      notify("已合并记忆", "success");
      setMergeDraft(null);
      setMergeContent("");
      await load();
    } catch (error) {
      notify(errorMessage(error), "error");
    } finally {
      setApplying(false);
    }
  };

  const applyLower = async (recommendation: ReviewRecommendation) => {
    if (recommendation.memory_ids.length !== 1) {
      notify("降权建议需要只包含一条记忆", "error");
      return;
    }
    const memoryId = recommendation.memory_ids[0];
    const memory = memoryMap.get(memoryId);
    if (!memory) {
      notify("未在当前活跃记忆中找到这条记忆", "error");
      return;
    }
    if (
      !(await confirm({
        title: "降低重要度",
        message: "确定要降低这条记忆的重要度吗？",
        tone: "warning",
        confirmLabel: "降低"
      }))
    ) {
      return;
    }
    setApplying(true);
    try {
      const nextImportance = Math.max(1, memory.importance - 1);
      await api.updateMemory(memoryId, { importance: nextImportance });
      notify("已降低记忆重要度", "success");
      await load();
    } catch (error) {
      notify(errorMessage(error), "error");
    } finally {
      setApplying(false);
    }
  };

  return (
    <div className="page-stack">
      <PageHeader
        title="记忆体检"
        subtitle="记忆体检只返回建议，不会自动修改数据。"
        action={
          <button
            className="primary-button"
            type="button"
            disabled={state.loading}
            onClick={() => load(true)}
          >
            <ListChecks size={16} />
            {state.loading ? "体检中" : "运行体检"}
          </button>
        }
      />
      {state.loading && <LoadingBlock label="正在运行体检" />}
      {state.error && <ErrorBlock message={state.error} onRetry={load} />}
      {state.data && (
        <>
          <div className="stats-grid">
            <StatCard label="扫描记忆" value={state.data.review.total} />
            <StatCard label="建议数量" value={state.data.review.recommendations.length} />
          </div>
          {state.data.review.recommendations.length === 0 && <EmptyBlock label="暂无体检建议" />}
          <div className="review-groups">
            {REVIEW_ACTIONS.map((action) => {
              const items = grouped.get(action) || [];
              if (items.length === 0) return null;
              return (
                <section className="panel" key={action}>
                  <div className="panel-header">
                    <h2>{reviewActionText(action)}</h2>
                    <span className="count-pill">{items.length}</span>
                  </div>
                  <div className="recommendation-list">
                    {items.map((recommendation, index) => (
                      <article className="recommendation-card" key={`${action}-${index}`}>
                        <div className="recommendation-topline">
                          {badge(recommendation.action)}
                          {badge(recommendation.relation)}
                        </div>
                        <p>{recommendation.reason}</p>
                        <FieldList
                          compact
                          entries={[
                            ["记忆 ID", recommendation.memory_ids],
                            ["建议内容", recommendation.suggested_content]
                          ]}
                        />
                        <div className="linked-memories">
                          {recommendation.memory_ids.map((id) => {
                            const memory = memoryMap.get(id);
                            return (
                              <div className="linked-memory" key={id}>
                                <strong>{shortId(id)}</strong>
                                <span>{memory?.content || "未在当前活跃记忆中找到"}</span>
                              </div>
                            );
                          })}
                        </div>
                        <div className="button-row">
                          {recommendation.action === "delete" && (
                            <button
                              className="danger-button"
                              type="button"
                              disabled={applying}
                              onClick={() => applyDelete(recommendation)}
                            >
                              移入回收站
                            </button>
                          )}
                          {recommendation.action === "merge" && (
                            <button
                              className="secondary-button"
                              type="button"
                              disabled={applying}
                              onClick={() => {
                                setMergeDraft(recommendation);
                                setMergeContent(recommendation.suggested_content || "");
                              }}
                            >
                              打开合并预览
                            </button>
                          )}
                          {recommendation.action === "lower" && (
                            <button
                              className="warning-button"
                              type="button"
                              disabled={applying || recommendation.memory_ids.length !== 1}
                              onClick={() => applyLower(recommendation)}
                            >
                              <ArrowDown size={16} />
                              应用降权
                            </button>
                          )}
                        </div>
                      </article>
                    ))}
                  </div>
                </section>
              );
            })}
          </div>
        </>
      )}
      {mergeDraft && (
        <Modal title="合并预览" onClose={() => setMergeDraft(null)}>
          <FieldList
            entries={[
              ["记忆 ID", mergeDraft.memory_ids],
              ["原因", mergeDraft.reason],
              ["建议内容", mergeDraft.suggested_content || "未提供，确认后由后端默认拼接"]
            ]}
          />
          <div className="linked-memories merge-preview-list">
            {mergeDraft.memory_ids.map((id) => {
              const memory = memoryMap.get(id);
              return (
                <div className="linked-memory" key={id}>
                  <strong>{shortId(id)}</strong>
                  <span>{memory?.content || "未在当前活跃记忆中找到"}</span>
                </div>
              );
            })}
          </div>
          <label className="field-block">
            <span>合并内容</span>
            <textarea
              value={mergeContent}
              onChange={(event) => setMergeContent(event.target.value)}
              rows={6}
            />
          </label>
          <div className="button-row end">
            <button className="ghost-button" type="button" onClick={() => setMergeDraft(null)}>
              取消
            </button>
            <button className="primary-button" type="button" disabled={applying} onClick={applyMerge}>
              确认合并
            </button>
          </div>
        </Modal>
      )}
    </div>
  );
}



