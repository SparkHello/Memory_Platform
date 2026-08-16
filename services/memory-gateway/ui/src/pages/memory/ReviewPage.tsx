import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ArrowDown,
  CheckCircle,
  Database,
  EyeOff,
  Layers3,
  ListChecks,
  Search,
  Sparkles
} from "lucide-react";
import { MemoryApi, isAbortError } from "../../api";
import type {
  CoreMemorySection,
  DatabaseHealthIssue,
  DatabaseHealthResult,
  DecisionLog,
  MemoryRecord,
  ProvidersStatus,
  ReviewRelatedCandidate,
  ReviewRecommendation,
  ReviewRevisionPreview,
  ReviewRiskTag,
  ReviewSeverity,
  ReviewResult
} from "../../types";
import { Badge, badge } from "../../components/Badge";
import { FieldList, FilterSelect } from "../../components/FormControls";
import { PageHeader } from "../../components/PageHeader";
import { InfoCard, StatCard } from "../../components/StatCard";
import { EmptyBlock, ErrorBlock, LoadingBlock } from "../../components/StateBlocks";
import { NextStepHint } from "../../components/NextStepHint";
import { Modal } from "../../components/Modal";
import type { ConfirmFn } from "../../hooks/useConfirm";
import type { LoadState } from "../../hooks/useAsyncData";
import { REVIEW_RISK_TAGS, REVIEW_SEVERITIES } from "../../utils/constants";
import {
  candidateSummary,
  dateText,
  displayText,
  errorMessage,
  sectionTitle,
  shortId
} from "../../utils/format";
import type { Notify } from "../pageTypes";

export function ReviewPage({
  api,
  notify,
  confirm,
  openMemory,
  setupStatus
}: {
  api: MemoryApi;
  notify: Notify;
  confirm: ConfirmFn;
  openMemory: (id: string) => void;
  setupStatus?: ProvidersStatus["setup"] | null;
}) {
  const [state, setState] = useState<
    LoadState<{ review: ReviewResult; health: DatabaseHealthResult; memories: MemoryRecord[]; logs: DecisionLog[] }>
  >({ loading: true, error: null, data: null });
  const [mergeDraft, setMergeDraft] = useState<ReviewRecommendation | null>(null);
  const [mergeContent, setMergeContent] = useState("");
  const [revisionDraft, setRevisionDraft] = useState<ReviewRecommendation | null>(null);
  const [revisionNote, setRevisionNote] = useState("");
  const [revisionPreview, setRevisionPreview] = useState<ReviewRevisionPreview | null>(null);
  const [relatedCandidates, setRelatedCandidates] = useState<ReviewRelatedCandidate[]>([]);
  const [selectedRelatedIds, setSelectedRelatedIds] = useState<Set<string>>(new Set());
  const [previewingRevision, setPreviewingRevision] = useState(false);
  const [loadingRelated, setLoadingRelated] = useState(false);
  const [coreImpactSections, setCoreImpactSections] = useState<CoreMemorySection[]>([]);
  const [consolidatingCore, setConsolidatingCore] = useState(false);
  const [applying, setApplying] = useState(false);
  const [dismissedKeys, setDismissedKeys] = useState<Set<string>>(new Set());
  const [riskFilter, setRiskFilter] = useState("all");
  const [severityFilter, setSeverityFilter] = useState("all");
  const [coreFilter, setCoreFilter] = useState("all");
  const [sortMode, setSortMode] = useState("severity_desc");
  const [showHealthInfo, setShowHealthInfo] = useState(false);

  const load = useCallback(async (showToast = false, signal?: AbortSignal) => {
    setState({ loading: true, error: null, data: null });
    try {
      const [review, health, memories, logs] = await Promise.all([
        api.reviewMemories(signal),
        api.memoryHealth(signal),
        api.listMemories({}, signal),
        api.decisionLogs(40, {}, signal)
      ]);
      setState({ loading: false, error: null, data: { review, health, memories, logs } });
      if (showToast) {
        notify(`体检完成，共 ${review.recommendations.length} 条建议`, "success");
      }
    } catch (error) {
      // 过期请求在 cleanup 里被 abort，直接丢弃，不覆盖新结果。
      if (isAbortError(error)) return;
      setState({ loading: false, error: errorMessage(error), data: null });
      if (showToast) {
        notify(errorMessage(error), "error");
      }
    }
  }, [api, notify]);

  useEffect(() => {
    const controller = new AbortController();
    void load(false, controller.signal);
    return () => controller.abort();
  }, [load]);

  const memoryMap = useMemo(() => {
    return new Map((state.data?.memories || []).map((memory) => [memory.id, memory]));
  }, [state.data?.memories]);

  const recommendations = useMemo(() => {
    return state.data?.review.recommendations || [];
  }, [state.data?.review.recommendations]);

  const healthIssues = useMemo(() => {
    return state.data?.health.issues || [];
  }, [state.data?.health.issues]);

  const visibleHealthIssues = useMemo(() => {
    return showHealthInfo
      ? healthIssues
      : healthIssues.filter((issue) => issue.severity !== "info");
  }, [healthIssues, showHealthInfo]);

  const visibleRecommendations = useMemo(() => {
    return recommendations
      .filter((recommendation) => !dismissedKeys.has(reviewDismissKey(recommendation)))
      .filter((recommendation) => {
        if (riskFilter !== "all" && !recommendation.risk_tags.includes(riskFilter as ReviewRiskTag)) {
          return false;
        }
        if (severityFilter !== "all" && recommendation.severity !== severityFilter) {
          return false;
        }
        if (coreFilter === "has_core_evidence" && recommendation.core_memory_sections.length === 0) {
          return false;
        }
        if (coreFilter === "no_core_evidence" && recommendation.core_memory_sections.length > 0) {
          return false;
        }
        return true;
      })
      .sort((left, right) => compareRecommendations(left, right, sortMode, memoryMap));
  }, [recommendations, dismissedKeys, riskFilter, severityFilter, coreFilter, sortMode, memoryMap]);

  const grouped = useMemo(() => {
    const map = new Map<ReviewSeverity, ReviewRecommendation[]>(
      REVIEW_SEVERITIES.map((severity) => [severity, []])
    );
    for (const recommendation of visibleRecommendations) {
      map.get(recommendation.severity)?.push(recommendation);
    }
    return map;
  }, [visibleRecommendations]);

  const riskStats = useMemo(() => {
    return REVIEW_RISK_TAGS.map((risk) => ({
      risk,
      count: recommendations.filter((recommendation) => recommendation.risk_tags.includes(risk)).length
    })).filter((item) => item.count > 0);
  }, [recommendations]);

  const highPriority = useMemo(() => {
    return recommendations
      .filter((recommendation) => recommendation.severity === "high")
      .filter((recommendation) => !dismissedKeys.has(reviewDismissKey(recommendation)))
      .slice(0, 3);
  }, [recommendations, dismissedKeys]);

  const coreRecommendationCount = useMemo(() => {
    return recommendations.filter((recommendation) => recommendation.core_memory_sections.length > 0).length;
  }, [recommendations]);

  const recentAiLogs = useMemo(() => {
    return (state.data?.logs || [])
      .filter((log) => decisionLogSource(log) === "review_modify")
      .slice(0, 3);
  }, [state.data?.logs]);

  const revisionMemoryIds = useMemo(() => {
    if (!revisionDraft) return [];
    return uniqueStrings([...revisionDraft.memory_ids, ...Array.from(selectedRelatedIds)]);
  }, [revisionDraft, selectedRelatedIds]);

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
      const result = await api.applyReviewAction({
        action: "move_to_trash",
        memoryIds: recommendation.memory_ids,
        reason: recommendation.reason,
        riskTags: recommendation.risk_tags,
        severity: recommendation.severity
      });
      setCoreImpactSections(result.affected_core_sections || []);
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
      const result = await api.applyReviewAction({
        action: "merge",
        memoryIds: mergeDraft.memory_ids,
        content: mergeContent.trim() || mergeDraft.suggested_content,
        reason: mergeDraft.reason,
        riskTags: mergeDraft.risk_tags,
        severity: mergeDraft.severity
      });
      setCoreImpactSections(result.affected_core_sections || []);
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
      const result = await api.applyReviewAction({
        action: "lower_importance",
        memoryIds: recommendation.memory_ids,
        reason: recommendation.reason,
        riskTags: recommendation.risk_tags,
        severity: recommendation.severity
      });
      setCoreImpactSections(result.affected_core_sections || []);
      notify("已降低记忆重要度", "success");
      await load();
    } catch (error) {
      notify(errorMessage(error), "error");
    } finally {
      setApplying(false);
    }
  };

  const applyConfirmReview = async (recommendation: ReviewRecommendation) => {
    const dismissKey = reviewDismissKey(recommendation);
    if (
      !(await confirm({
        title: "确认记忆仍然有效",
        message: "确认该记忆仍然有效？将 15 天后再次复核",
        tone: "default",
        confirmLabel: "确认"
      }))
    ) {
      return;
    }
    setApplying(true);
    try {
      const reviewAfter = new Date(Date.now() + 15 * 86400 * 1000).toISOString();
      const result = await api.applyReviewAction({
        action: "confirm_valid",
        memoryIds: recommendation.memory_ids,
        reason: recommendation.reason,
        riskTags: recommendation.risk_tags,
        severity: recommendation.severity,
        reviewAfter
      });
      setCoreImpactSections(result.affected_core_sections || []);
      notify("已确认，15 天后再次复核", "success");
      setDismissedKeys((prev) => {
        const next = new Set(prev);
        next.add(dismissKey);
        return next;
      });
    } catch (error) {
      notify(errorMessage(error), "error");
    } finally {
      setApplying(false);
    }
  };

  const applySnooze = async (recommendation: ReviewRecommendation) => {
    const dismissKey = reviewDismissKey(recommendation);
    setApplying(true);
    try {
      const reviewAfter = new Date(Date.now() + 15 * 86400 * 1000).toISOString();
      const result = await api.applyReviewAction({
        action: "snooze",
        memoryIds: recommendation.memory_ids,
        reason: recommendation.reason,
        riskTags: recommendation.risk_tags,
        severity: recommendation.severity,
        reviewAfter
      });
      setCoreImpactSections(result.affected_core_sections || []);
      notify("已稍后提醒，15 天后再看", "success");
      setDismissedKeys((prev) => {
        const next = new Set(prev);
        next.add(dismissKey);
        return next;
      });
      await load();
    } catch (error) {
      notify(errorMessage(error), "error");
    } finally {
      setApplying(false);
    }
  };

  const openRevisionDialog = (recommendation: ReviewRecommendation) => {
    setRevisionDraft(recommendation);
    setRevisionNote("");
    setRevisionPreview(null);
    setRelatedCandidates([]);
    setSelectedRelatedIds(new Set());
  };

  const closeRevisionDialog = () => {
    setRevisionDraft(null);
    setRevisionNote("");
    setRevisionPreview(null);
    setRelatedCandidates([]);
    setSelectedRelatedIds(new Set());
    setPreviewingRevision(false);
    setLoadingRelated(false);
  };

  const findRelatedMemories = async () => {
    if (!revisionDraft) return;
    const note = revisionNote.trim();
    if (!note) {
      notify("请先写下真实情况或修改说明", "error");
      return;
    }
    setLoadingRelated(true);
    try {
      const candidates = await api.findReviewRevisionRelated({
        memoryIds: revisionDraft.memory_ids,
        userNote: note,
        recommendationReason: revisionDraft.reason,
        suggestedContent: revisionDraft.suggested_content,
        limit: 8
      });
      setRelatedCandidates(candidates);
      setSelectedRelatedIds(new Set());
      setRevisionPreview(null);
      notify(candidates.length ? `找到 ${candidates.length} 条相关记忆` : "没有找到可加入的相关记忆", "success");
    } catch (error) {
      notify(errorMessage(error), "error");
    } finally {
      setLoadingRelated(false);
    }
  };

  const toggleRelatedCandidate = (memoryId: string) => {
    setSelectedRelatedIds((prev) => {
      const next = new Set(prev);
      if (next.has(memoryId)) {
        next.delete(memoryId);
      } else {
        next.add(memoryId);
      }
      return next;
    });
    setRevisionPreview(null);
  };

  const generateRevisionPreview = async () => {
    if (!revisionDraft) return;
    const note = revisionNote.trim();
    if (!note) {
      notify("请先写下真实情况或修改说明", "error");
      return;
    }
    setPreviewingRevision(true);
    try {
      const preview = await api.previewReviewRevision({
        memoryIds: revisionMemoryIds,
        userNote: note,
        recommendationReason: revisionDraft.reason,
        relation: revisionDraft.relation,
        suggestedContent: revisionDraft.suggested_content,
        riskTags: revisionDraft.risk_tags,
        severity: revisionDraft.severity
      });
      setRevisionPreview(preview);
      notify("已生成修改预览", "success");
    } catch (error) {
      notify(errorMessage(error), "error");
    } finally {
      setPreviewingRevision(false);
    }
  };

  const applyRevisionPreview = async () => {
    if (!revisionDraft || !revisionPreview) return;
    if (
      !(await confirm({
        title: "应用 AI 修改",
        message:
          "确认应用这组修改？可能会更新、合并或将部分记忆移入回收站；更新或合并后的记忆会按类型设置下一次复核时间。",
        tone: "warning",
        confirmLabel: "应用修改"
      }))
    ) {
      return;
    }
    const dismissKey = reviewDismissKey(revisionDraft);
    setApplying(true);
    try {
      const result = await api.applyReviewRevision({
        memoryIds: revisionMemoryIds,
        operations: revisionPreview.operations,
        previewToken: revisionPreview.preview_token,
        riskTags: revisionDraft.risk_tags,
        severity: revisionDraft.severity
      });
      setCoreImpactSections(result.affected_core_sections || []);
      notify("已应用 AI 修改，并设置后续复核", "success");
      setDismissedKeys((prev) => {
        const next = new Set(prev);
        next.add(dismissKey);
        return next;
      });
      closeRevisionDialog();
      await load();
    } catch (error) {
      notify(errorMessage(error), "error");
    } finally {
      setApplying(false);
    }
  };

  const consolidateCoreMemory = async () => {
    if (
      !(await confirm({
        title: "重新整理核心记忆",
        message:
          "确认重新整理核心记忆？该操作只保存能通过逐字证据校验的内容；模型可能漏掉部分分区，请在完成后复核结果。",
        tone: "warning",
        confirmLabel: "重新整理"
      }))
    ) {
      return;
    }
    setConsolidatingCore(true);
    try {
      await api.consolidateCoreMemory();
      setCoreImpactSections([]);
      notify("已运行核心记忆整理", "success");
      await load();
    } catch (error) {
      notify(errorMessage(error), "error");
    } finally {
      setConsolidatingCore(false);
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
      {state.error && <ErrorBlock message={state.error} onRetry={() => void load()} />}
      {state.data && (
        <>
          {coreImpactSections.length > 0 && (
            <div className="notice warning core-impact-notice">
              <Layers3 size={18} />
              <span>
                这次修改影响了核心记忆证据：
                {coreImpactSections.map((section) => sectionTitle(section.section)).join("、")}
              </span>
              <button
                className="secondary-button"
                type="button"
                disabled={consolidatingCore}
                onClick={consolidateCoreMemory}
              >
                <Layers3 size={16} />
                {consolidatingCore ? "整理中" : "整理核心记忆"}
              </button>
            </div>
          )}
          {state.data.review.recommendations.length === 0 && (
            <>
              <NextStepHint setup={setupStatus} />
              <EmptyBlock label="暂无体检建议" />
            </>
          )}
          <div className="stats-grid">
            <StatCard label="扫描记忆" value={state.data.review.total} />
            <StatCard label="建议数量" value={state.data.review.recommendations.length} />
            <StatCard label="高优先级" value={highPriority.length} />
            <StatCard label="核心影响" value={coreRecommendationCount} />
          </div>
          {state.data.review.recommendations.length > 0 && (
            <section className="panel review-toolbar-panel">
              <div className="toolbar review-toolbar">
                <FilterSelect
                  label="风险类型"
                  value={riskFilter}
                  options={["all", ...REVIEW_RISK_TAGS]}
                  onChange={setRiskFilter}
                />
                <label className="field-block">
                  <span>严重程度</span>
                  <select value={severityFilter} onChange={(event) => setSeverityFilter(event.target.value)}>
                    <option value="all">全部</option>
                    {REVIEW_SEVERITIES.map((severity) => (
                      <option value={severity} key={severity}>
                        {severityText(severity)}
                      </option>
                    ))}
                  </select>
                </label>
                <FilterSelect
                  label="核心影响"
                  value={coreFilter}
                  options={["all", "has_core_evidence", "no_core_evidence"]}
                  onChange={setCoreFilter}
                />
                <FilterSelect
                  label="排序"
                  value={sortMode}
                  options={["severity_desc", "updated_desc"]}
                  onChange={setSortMode}
                />
              </div>
            </section>
          )}
          {state.data.review.recommendations.length > 0 && visibleRecommendations.length === 0 && (
            <EmptyBlock label="当前筛选下暂无体检建议" />
          )}
          <div className="review-groups">
            {[...REVIEW_SEVERITIES].reverse().map((severity) => {
              const items = grouped.get(severity) || [];
              if (items.length === 0) return null;
              return (
                <section className={`panel severity-queue severity-queue-${severity}`} key={severity}>
                  <div className="panel-header">
                    <h2>{severityText(severity)}严重度行动队列</h2>
                    <span className="count-pill">{items.length}</span>
                  </div>
                  <div className="recommendation-list">
                    {items.map((recommendation, index) => (
                      <article className="recommendation-card" key={`${severity}-${index}`}>
                        <div className="recommendation-topline">
                          {badge(recommendation.action)}
                          {badge(recommendation.relation)}
                          <span className={`severity-pill severity-${recommendation.severity}`}>
                            {severityText(recommendation.severity)}
                          </span>
                          {recommendation.risk_tags.map((risk) => (
                            <Badge key={risk} value={risk} />
                          ))}
                          {recommendation.core_memory_sections.length > 0 && (
                            <span className="core-pill">
                              核心：{recommendation.core_memory_sections.map(sectionTitle).join("、")}
                            </span>
                          )}
                        </div>
                        <p>{recommendation.reason}</p>
                        <FieldList
                          compact
                          entries={[
                            ["建议内容", recommendation.suggested_content],
                            ["可执行操作", recommendation.next_action_options.map(displayText)]
                          ]}
                        />
                        <div className="linked-memories">
                          {recommendation.memory_ids.map((id) => {
                            const memory = memoryMap.get(id);
                            return (
                              <button
                                className="linked-memory linked-memory-button"
                                type="button"
                                key={id}
                                onClick={() => openMemory(id)}
                                title="打开记忆档案"
                              >
                                <strong>{shortId(id)}</strong>
                                <span>{memory?.content || "未在当前活跃记忆中找到"}</span>
                              </button>
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
                              title={
                                recommendation.memory_ids.length !== 1
                                  ? "降权建议需要只包含一条记忆"
                                  : undefined
                              }
                              onClick={() => applyLower(recommendation)}
                            >
                              <ArrowDown size={16} />
                              应用降权
                            </button>
                          )}
                          {recommendation.next_action_options.includes("ai_modify") && (
                            <button
                              className="secondary-button"
                              type="button"
                              disabled={applying}
                              onClick={() => openRevisionDialog(recommendation)}
                            >
                              <Sparkles size={16} />
                              AI 修改
                            </button>
                          )}
                          {recommendation.next_action_options.includes("confirm_valid") && (
                            <button
                              className="primary-button"
                              type="button"
                              disabled={applying}
                              onClick={() => applyConfirmReview(recommendation)}
                            >
                              <CheckCircle size={16} />
                              已确认，15天后复核
                            </button>
                          )}
                          {recommendation.next_action_options.includes("snooze") && (
                            <button
                              className="ghost-button"
                              type="button"
                              disabled={applying}
                              onClick={() => applySnooze(recommendation)}
                            >
                              <EyeOff size={16} />
                              稍后提醒
                            </button>
                          )}
                          {recommendation.next_action_options.includes("review_core_memory") && (
                            <button
                              className="secondary-button"
                              type="button"
                              disabled={consolidatingCore}
                              onClick={consolidateCoreMemory}
                            >
                              <Layers3 size={16} />
                              整理核心记忆
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

          <section className="panel panel--quiet governance-overview">
            <div className="panel-header">
              <h2>治理概览</h2>
              <span className="count-pill">当前显示 {visibleRecommendations.length}</span>
            </div>
            <div className="governance-grid">
              <div className="governance-block">
                <strong>风险统计</strong>
                {riskStats.length === 0 ? (
                  <p className="muted-line">暂无风险标签</p>
                ) : (
                  <div className="risk-stat-list">
                    {riskStats.map((item) => (
                      <button
                        className={`risk-stat ${riskFilter === item.risk ? "active" : ""}`}
                        type="button"
                        key={item.risk}
                        onClick={() => setRiskFilter(riskFilter === item.risk ? "all" : item.risk)}
                      >
                        <span>{displayText(item.risk)}</span>
                        <strong>{item.count}</strong>
                      </button>
                    ))}
                  </div>
                )}
              </div>
              <div className="governance-block">
                <strong>高优先级待处理</strong>
                {highPriority.length === 0 ? (
                  <p className="muted-line">暂无高优先级建议</p>
                ) : (
                  <div className="mini-review-list">
                    {highPriority.map((recommendation) => (
                      <button
                        className="mini-review-item"
                        type="button"
                        key={reviewDismissKey(recommendation)}
                        onClick={() => {
                          setSeverityFilter("high");
                          setRiskFilter("all");
                        }}
                      >
                        <span>{displayText(recommendation.risk_tags[0] || recommendation.action)}</span>
                        <em>{recommendation.reason}</em>
                      </button>
                    ))}
                  </div>
                )}
              </div>
              <div className="governance-block">
                <strong>最近 AI 修改</strong>
                {recentAiLogs.length === 0 ? (
                  <p className="muted-line">暂无近期 AI 修改记录</p>
                ) : (
                  <div className="mini-review-list">
                    {recentAiLogs.map((log) => (
                      <div className="mini-review-item passive" key={log.id}>
                        <span>{dateText(log.created_at)}</span>
                        <em>{candidateSummary(log.candidate_json)}</em>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>
          </section>
          <section className="panel panel--quiet governance-overview">
            <div className="panel-header">
              <h2>数据库健康</h2>
              <span className="count-pill">{displayText(state.data.health.status)}</span>
            </div>
            <div className="stats-grid compact-stats">
              <StatCard label="错误" value={state.data.health.summary.errors} />
              <StatCard label="警告" value={state.data.health.summary.warnings} />
              <StatCard label="提示" value={state.data.health.summary.info} />
              <InfoCard label="检查时间" value={dateText(state.data.health.checked_at)} />
            </div>
            <div className="button-row">
              <button
                className="secondary-button"
                type="button"
                onClick={() => setShowHealthInfo((value) => !value)}
              >
                <Database size={16} />
                {showHealthInfo ? "隐藏提示" : "显示提示"}
              </button>
              <span className="muted-line">
                当前显示 {visibleHealthIssues.length} / {healthIssues.length}
              </span>
            </div>
            {visibleHealthIssues.length === 0 ? (
              <EmptyBlock
                label={healthIssues.length === 0 ? "数据库结构暂无风险" : "仅有提示项，当前已隐藏"}
              />
            ) : (
              <div className="recommendation-list">
                {visibleHealthIssues.map((issue) => (
                  <article className="recommendation-card" key={healthIssueKey(issue)}>
                    <div className="recommendation-topline">
                      {badge(issue.type)}
                      <span className={`severity-pill ${healthSeverityClass(issue.severity)}`}>
                        {displayText(issue.severity)}
                      </span>
                      <span className="count-pill">{issue.object_id}</span>
                    </div>
                    <p>{issue.message}</p>
                    <FieldList
                      compact
                      entries={[
                        ["关联 ID", issue.related_id],
                        ["建议动作", issue.recommended_action]
                      ]}
                    />
                  </article>
                ))}
              </div>
            )}
          </section>
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
      {revisionDraft && (
        <Modal title="AI 修改预览" onClose={closeRevisionDialog}>
          <div className="revision-dialog">
            <FieldList
              entries={[
                ["原建议记忆", revisionDraft.memory_ids],
                ["体检原因", revisionDraft.reason],
                ["建议内容", revisionDraft.suggested_content || "未提供"]
              ]}
            />
            <div className="recommendation-topline">
              <strong>本次修改范围</strong>
              <span className="count-pill">{revisionMemoryIds.length} 条</span>
            </div>
            <div className="linked-memories merge-preview-list">
              {revisionMemoryIds.map((id) => {
                const memory = memoryMap.get(id);
                const isRelated = !revisionDraft.memory_ids.includes(id);
                return (
                  <div className="linked-memory" key={id}>
                    <strong>{shortId(id)}</strong>
                    <span>
                      <small>{isRelated ? "关联记忆" : "体检记忆"}</small>
                      <em>{memory?.content || "未在当前活跃记忆中找到"}</em>
                    </span>
                  </div>
                );
              })}
            </div>
            <label className="field-block">
              <span>真实情况或修改说明</span>
              <textarea
                value={revisionNote}
                onChange={(event) => {
                  setRevisionNote(event.target.value);
                  setRevisionPreview(null);
                  setRelatedCandidates([]);
                  setSelectedRelatedIds(new Set());
                }}
                rows={5}
                placeholder="例如：现在用 ChatWise，Kelivo 那条过期了。"
              />
            </label>
            {relatedCandidates.length > 0 && (
              <div className="related-candidates">
                <div className="recommendation-topline">
                  <strong>相关记忆候选</strong>
                  <span className="count-pill">已选 {selectedRelatedIds.size}</span>
                </div>
                {relatedCandidates.map((candidate) => (
                  <label className="related-candidate" key={candidate.memory.id}>
                    <input
                      type="checkbox"
                      checked={selectedRelatedIds.has(candidate.memory.id)}
                      onChange={() => toggleRelatedCandidate(candidate.memory.id)}
                    />
                    <span>
                      <strong>{shortId(candidate.memory.id)}</strong>
                      <em>{candidate.memory.content}</em>
                      <small>
                        {displayText(candidate.relation)} · {Math.round(candidate.score * 100)}% ·{" "}
                        {candidate.channels.join(", ")}
                        {candidate.is_core_memory_evidence
                          ? ` · 核心证据：${candidate.core_memory_sections
                              .map((section) => sectionTitle(section.section))
                              .join("、")}`
                          : ""}
                      </small>
                      <small>{candidate.reason}</small>
                    </span>
                  </label>
                ))}
              </div>
            )}
            <div className="button-row">
              <button
                className="secondary-button"
                type="button"
                disabled={loadingRelated}
                onClick={findRelatedMemories}
              >
                <Search size={16} />
                {loadingRelated ? "查找中" : "查找相关记忆"}
              </button>
              <button
                className="secondary-button"
                type="button"
                disabled={previewingRevision}
                onClick={generateRevisionPreview}
              >
                <Sparkles size={16} />
                {previewingRevision ? "生成中" : "生成修改预览"}
              </button>
              <span className="count-pill">本次可修改 {revisionMemoryIds.length} 条</span>
            </div>
            {revisionPreview && (
              <div className="revision-preview-list">
                {revisionPreview.reason && <p className="muted-line">{revisionPreview.reason}</p>}
                {revisionPreview.operations.map((operation, index) => (
                  <article className="revision-operation" key={`${operation.operation}-${index}`}>
                    <div className="recommendation-topline">
                      <strong>{reviewRevisionOperationText(operation.operation)}</strong>
                      {operation.review_policy && (
                        <span className="count-pill">{operation.review_policy.interval_days} 天后复核</span>
                      )}
                    </div>
                    <FieldList
                      compact
                      entries={[
                        ["目标记忆", operation.target_memory_id],
                        ["关联记忆", operation.memory_ids],
                        ["处理结果", operation.operation === "archive" ? "将移入回收站" : null],
                        ["预览内容", operation.content],
                        ["原因", operation.reason],
                        [
                          "下次复核",
                          operation.review_policy
                            ? `${dateText(operation.review_policy.review_after)} · ${operation.review_policy.reason}`
                            : null
                        ]
                      ]}
                    />
                  </article>
                ))}
              </div>
            )}
            <div className="button-row end">
              <button className="ghost-button" type="button" onClick={closeRevisionDialog}>
                取消
              </button>
              <button
                className="primary-button"
                type="button"
                disabled={applying || !revisionPreview}
                onClick={applyRevisionPreview}
              >
                应用修改
              </button>
            </div>
          </div>
        </Modal>
      )}
    </div>
  );
}

function reviewDismissKey(recommendation: ReviewRecommendation): string {
  return `review-${recommendation.action}-${recommendation.memory_ids.join("-")}`;
}

function healthIssueKey(issue: DatabaseHealthIssue): string {
  return `health-${issue.type}-${issue.object_id}-${issue.related_id || "none"}`;
}

function healthSeverityClass(severity: DatabaseHealthIssue["severity"]): string {
  if (severity === "error") return "severity-high";
  if (severity === "warning") return "severity-medium";
  return "severity-low";
}

function compareRecommendations(
  left: ReviewRecommendation,
  right: ReviewRecommendation,
  sortMode: string,
  memoryMap: Map<string, MemoryRecord>
): number {
  if (sortMode === "updated_desc") {
    return latestRecommendationUpdatedAt(right, memoryMap) - latestRecommendationUpdatedAt(left, memoryMap);
  }
  const severityDiff = severityRank(right.severity) - severityRank(left.severity);
  if (severityDiff !== 0) return severityDiff;
  return latestRecommendationUpdatedAt(right, memoryMap) - latestRecommendationUpdatedAt(left, memoryMap);
}

function latestRecommendationUpdatedAt(
  recommendation: ReviewRecommendation,
  memoryMap: Map<string, MemoryRecord>
): number {
  const timestamps = recommendation.memory_ids
    .map((id) => memoryMap.get(id)?.updated_at)
    .filter(Boolean)
    .map((value) => new Date(value || "").getTime())
    .filter((value) => !Number.isNaN(value));
  return timestamps.length ? Math.max(...timestamps) : 0;
}

function severityRank(severity: ReviewSeverity): number {
  return { low: 1, medium: 2, high: 3 }[severity];
}

function severityText(severity: ReviewSeverity): string {
  return { low: "低", medium: "中", high: "高" }[severity];
}

function decisionLogSource(log: DecisionLog): string {
  try {
    const parsed = JSON.parse(log.candidate_json) as { source?: string };
    return parsed.source || "";
  } catch {
    return "";
  }
}

function reviewRevisionOperationText(operation: string): string {
  return {
    update: "更新记忆",
    merge: "合并记忆",
    archive: "移入回收站",
    no_change: "暂不修改"
  }[operation] || operation;
}

function uniqueStrings(values: string[]): string[] {
  return Array.from(new Set(values));
}
