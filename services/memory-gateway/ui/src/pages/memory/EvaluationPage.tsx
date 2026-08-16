import {
  BarChart3,
  Check,
  CircleDashed,
  DatabaseZap,
  ListChecks,
  Play,
  Plus,
  RefreshCcw,
  Save,
  SearchX,
  Trash2
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { isAbortError, type MemoryApi } from "../../api";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { PageHeader } from "../../components/PageHeader";
import { EmptyBlock, ErrorBlock, LoadingBlock } from "../../components/StateBlocks";
import { useConfirm } from "../../hooks/useConfirm";
import { useUnsavedChangesGuard } from "../../hooks/useUnsavedChangesGuard";
import type {
  MechanismDiagnosisResult,
  MechanismVerdict,
  MemoryRecord,
  RecallEvalJudgment,
  RecallEvalLabel,
  RecallEvalRunResult,
  RecallEvalValidationIssue,
  RecallEvalWorkbench
} from "../../types";
import { displayText, errorMessage, numberText, shortId } from "../../utils/format";

type Notify = (message: string, kind?: "success" | "error" | "info") => void;
type RunMode = "keyword" | "embedding";

type EvalState = {
  loading: boolean;
  error: string | null;
  diagnosis: MechanismDiagnosisResult | null;
  diagnosisError: string | null;
  workbench: RecallEvalWorkbench | null;
};

const EMPTY_STATE: EvalState = {
  loading: true,
  error: null,
  diagnosis: null,
  diagnosisError: null,
  workbench: null
};

const JUDGMENT_OPTIONS: Array<{
  value: RecallEvalJudgment;
  label: string;
  icon: typeof CircleDashed;
}> = [
  { value: "unlabeled", label: "未标注", icon: CircleDashed },
  { value: "relevant", label: "有相关记忆", icon: ListChecks },
  { value: "no_answer", label: "无答案", icon: SearchX }
];

function normalizedJudgment(label: RecallEvalLabel): RecallEvalJudgment {
  return label.judgment || (label.relevant_ids.length > 0 ? "relevant" : "unlabeled");
}

function isGraded(label: RecallEvalLabel): boolean {
  const judgment = normalizedJudgment(label);
  return judgment === "no_answer" || (judgment === "relevant" && label.relevant_ids.length > 0);
}

function labelStatusText(label: RecallEvalLabel): string {
  const judgment = normalizedJudgment(label);
  if (judgment === "no_answer") return "无答案";
  if (judgment === "relevant") {
    return label.relevant_ids.length > 0 ? `${label.relevant_ids.length} 条相关记忆` : "待选择相关记忆";
  }
  return "未标注";
}

function retrievalModeText(mode: string | undefined): string {
  const labels: Record<string, string> = {
    keyword: "关键词",
    embedding: "语义",
    hybrid: "混合",
    keyword_fallback: "关键词回退",
    none: "无结果"
  };
  return labels[mode || ""] || mode || "未知";
}

export function EvaluationPage({ api, notify }: { api: MemoryApi; notify: Notify }) {
  const [state, setState] = useState<EvalState>(EMPTY_STATE);
  const [labels, setLabels] = useState<RecallEvalLabel[]>([]);
  const [selectedLabelId, setSelectedLabelId] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [initializing, setInitializing] = useState(false);
  const [runningMode, setRunningMode] = useState<RunMode | null>(null);
  // 最近一次载入/保存后的标注快照，用于判断当前标注是否有未保存修改。
  const [savedSignature, setSavedSignature] = useState("[]");
  const { confirm, confirmState, resolveConfirm } = useConfirm();

  const load = useCallback(async (signal?: AbortSignal) => {
    setState((current) => ({ ...current, loading: true, error: null }));
    let diagnosisError: string | null = null;
    try {
      const diagnosis = await api.evaluationDiagnosis(signal).catch((error) => {
        // 诊断失败不应连累整页：记下错误，仍渲染召回工作台。
        diagnosisError = errorMessage(error);
        return null;
      });
      // 快照明确未初始化时 workbench 必 404：跳过请求，直接渲染初始化空态；
      // 诊断失败或字段缺失（旧后端）时保持原行为，由 404 静默逻辑兜底。
      const snapshotUninitialized = diagnosis !== null && diagnosis.snapshot_initialized === false;
      const workbench = snapshotUninitialized
        ? null
        : await api.recallEvaluationWorkbench({}, signal).catch((error) => {
            if (String(errorMessage(error)).startsWith("404:")) return null;
            throw error;
          });
      setState({ loading: false, error: null, diagnosis, diagnosisError, workbench });
      const nextLabels = workbench?.labels || [];
      setLabels(nextLabels);
      setSavedSignature(JSON.stringify(nextLabels));
      setSelectedLabelId((current) => current || workbench?.labels[0]?.id || null);
    } catch (error) {
      // 过期请求在 cleanup 里被 abort，直接丢弃，不覆盖新结果。
      if (isAbortError(error)) return;
      setState({ loading: false, error: errorMessage(error), diagnosis: null, diagnosisError, workbench: null });
    }
  }, [api]);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const selectedLabel = useMemo(() => {
    return labels.find((label) => label.id === selectedLabelId) || labels[0] || null;
  }, [labels, selectedLabelId]);

  const candidateMap = useMemo(() => {
    return new Map((state.workbench?.candidates || []).map((candidate) => [candidate.id, candidate]));
  }, [state.workbench?.candidates]);

  const validationByLabel = useMemo(() => {
    const map = new Map<string, RecallEvalValidationIssue[]>();
    for (const issue of state.workbench?.validation_issues || []) {
      const key = issue.label_id || "_global";
      const bucket = map.get(key) || [];
      bucket.push(issue);
      map.set(key, bucket);
    }
    return map;
  }, [state.workbench?.validation_issues]);

  const labelsDirty = useMemo(() => JSON.stringify(labels) !== savedSignature, [labels, savedSignature]);
  useUnsavedChangesGuard(labelsDirty, "当前标注有未保存的修改，离开后这些修改会丢失。确定要离开吗？", confirm);

  const gradedCount = labels.filter(isGraded).length;
  const relevantCount = labels.filter(
    (label) => normalizedJudgment(label) === "relevant" && label.relevant_ids.length > 0
  ).length;
  const noAnswerCount = labels.filter((label) => normalizedJudgment(label) === "no_answer").length;
  const targetMin = state.workbench?.target_label_min || 20;
  const targetMax = state.workbench?.target_label_max || 30;
  const progress = Math.min(100, Math.round((gradedCount / targetMin) * 100));

  const initialize = async () => {
    setInitializing(true);
    try {
      await api.initRecallEvaluation();
      notify("评测快照已刷新", "success");
      await load();
    } catch (error) {
      notify(errorMessage(error), "error");
    } finally {
      setInitializing(false);
    }
  };

  const save = async () => {
    setSaving(true);
    try {
      const result = await api.saveRecallEvaluationLabels(labels);
      notify(`已保存 ${result.summary.queries_total} 条 query 标注`, "success");
      await load();
    } catch (error) {
      notify(errorMessage(error), "error");
    } finally {
      setSaving(false);
    }
  };

  const run = async (mode: RunMode) => {
    setRunningMode(mode);
    try {
      // 评测读取磁盘上的 labels.jsonl，先落盘当前标注，保证跑的就是界面所见。
      await api.saveRecallEvaluationLabels(labels);
      await api.runRecallEvaluation({ mode, k: 8 });
      notify(`${displayText(mode)}基线已完成`, "success");
      await load();
    } catch (error) {
      notify(errorMessage(error), "error");
    } finally {
      setRunningMode(null);
    }
  };

  const addLabel = () => {
    const used = new Set(labels.map((label) => label.id));
    let nextIndex = labels.length + 1;
    let id = `q${String(nextIndex).padStart(3, "0")}`;
    while (used.has(id)) {
      nextIndex += 1;
      id = `q${String(nextIndex).padStart(3, "0")}`;
    }
    const next: RecallEvalLabel = { id, query: "", judgment: "unlabeled", relevant_ids: [], note: "" };
    setLabels((current) => [...current, next]);
    setSelectedLabelId(id);
  };

  const removeLabel = async (id: string) => {
    const confirmed = await confirm({
      title: "删除标注",
      message: `确认删除标注 ${id}？保存前可通过重新载入恢复，保存后将从评测快照中移除。`,
      tone: "danger",
      confirmLabel: "删除"
    });
    if (!confirmed) return;
    setLabels((current) => current.filter((label) => label.id !== id));
    setSelectedLabelId((current) => (current === id ? labels.find((label) => label.id !== id)?.id || null : current));
  };

  const updateSelected = (patch: Partial<RecallEvalLabel>) => {
    if (!selectedLabel) return;
    setLabels((current) =>
      current.map((label) => (label.id === selectedLabel.id ? { ...label, ...patch } : label))
    );
  };

  const setJudgment = (judgment: RecallEvalJudgment) => {
    updateSelected({
      judgment,
      relevant_ids: judgment === "relevant" ? selectedLabel?.relevant_ids || [] : []
    });
  };

  const toggleRelevant = (memoryId: string) => {
    if (!selectedLabel) return;
    const current = new Set(selectedLabel.relevant_ids);
    if (current.has(memoryId)) {
      current.delete(memoryId);
    } else {
      current.add(memoryId);
    }
    updateSelected({ judgment: "relevant", relevant_ids: Array.from(current) });
  };

  const results = state.workbench?.last_results || {};

  return (
    <div className="page-stack evaluation-page">
      <PageHeader
        title="评测闭环"
        subtitle="用真实 query 标注和机制诊断验证当前记忆系统是否真的被数据驱动。"
        action={
          <div className="button-row end">
            {labelsDirty && <span className="count-pill">有未保存修改</span>}
            <button className="secondary-button" type="button" onClick={() => void load()}>
              <RefreshCcw size={16} />
              {labelsDirty ? "放弃并刷新" : "刷新"}
            </button>
            <button
              className="primary-button"
              type="button"
              onClick={initialize}
              disabled={initializing}
            >
              <DatabaseZap size={16} />
              {initializing ? "初始化中" : "初始化快照"}
            </button>
          </div>
        }
      />

      {state.loading && <LoadingBlock label="正在载入评测闭环" />}
      {state.error && <ErrorBlock message={state.error} onRetry={() => void load()} />}
      {state.diagnosisError && !state.diagnosis && (
        <ErrorBlock message={`机制诊断加载失败：${state.diagnosisError}`} onRetry={() => void load()} />
      )}

      {state.diagnosis && (
        <section className="evaluation-verdict-grid">
          {state.diagnosis.verdicts.map((verdict) => (
            <VerdictTile verdict={verdict} key={verdict.mechanism} />
          ))}
        </section>
      )}

      <section className="panel evaluation-progress-panel">
        <div className="panel-header compact-header">
          <div>
            <h2>召回标注进度</h2>
            <p className="muted-line">
              已标注 {gradedCount} / {targetMin} 条（相关 {relevantCount}，无答案 {noAnswerCount}），建议首批保持在 {targetMin}-{targetMax} 条。
            </p>
          </div>
          <span className="count-pill">{progress}%</span>
        </div>
        <div className="evaluation-progress-bar" aria-label="召回标注进度">
          <span style={{ transform: `scaleX(${progress / 100})` }} />
        </div>
        {(state.workbench?.validation_issues || []).length > 0 && (
          <div className="evaluation-issues">
            {state.workbench?.validation_issues.map((issue) => (
              <span key={`${issue.code}-${issue.label_id}-${issue.memory_id}`}>
                {issue.label_id ? `${issue.label_id}: ` : ""}
                {issue.message}
              </span>
            ))}
          </div>
        )}
      </section>

      {state.workbench ? (
        <section className="evaluation-layout">
          <div className="panel evaluation-label-panel">
            <div className="panel-header compact-header">
              <h2>Query 标注</h2>
              <button className="secondary-button compact" type="button" onClick={addLabel}>
                <Plus size={15} />
                新增
              </button>
            </div>
            {labels.length === 0 ? (
              <EmptyBlock label="暂无 query，先新增一条标注" compact />
            ) : (
              <div className="evaluation-label-list">
                {labels.map((label) => {
                  const issues = validationByLabel.get(label.id) || [];
                  return (
                    <button
                      className={`evaluation-label-item ${selectedLabel?.id === label.id ? "active" : ""}`}
                      type="button"
                      key={label.id}
                      onClick={() => setSelectedLabelId(label.id)}
                    >
                      <strong>{label.id}</strong>
                      <span>{label.query || "未填写 query"}</span>
                      <em className={`evaluation-label-status ${isGraded(label) ? "graded" : ""}`}>
                        {labelStatusText(label)}
                      </em>
                      {issues.length > 0 && <small>{issues.length} 个问题</small>}
                    </button>
                  );
                })}
              </div>
            )}
          </div>

          <div className="panel evaluation-editor-panel">
            {selectedLabel ? (
              <>
                <div className="panel-header compact-header">
                  <h2>{selectedLabel.id}</h2>
                  <button
                    className="danger-button compact"
                    type="button"
                    onClick={() => void removeLabel(selectedLabel.id)}
                  >
                    <Trash2 size={15} />
                    删除
                  </button>
                </div>
                <label className="field-block">
                  <span>检索意图</span>
                  <textarea
                    value={selectedLabel.query}
                    rows={3}
                    onChange={(event) => updateSelected({ query: event.target.value })}
                    placeholder="例如：用户的饮食偏好"
                  />
                </label>
                <div className="field-block">
                  <span>标注判断</span>
                  <div className="evaluation-judgment-control" role="radiogroup" aria-label="标注判断">
                    {JUDGMENT_OPTIONS.map((option) => {
                      const Icon = option.icon;
                      const active = normalizedJudgment(selectedLabel) === option.value;
                      return (
                        <button
                          className={active ? "active" : ""}
                          type="button"
                          role="radio"
                          aria-checked={active}
                          key={option.value}
                          onClick={() => setJudgment(option.value)}
                        >
                          <Icon size={15} />
                          <span>{option.label}</span>
                        </button>
                      );
                    })}
                  </div>
                </div>
                <label className="field-block">
                  <span>说明</span>
                  <input
                    value={selectedLabel.note || ""}
                    onChange={(event) => updateSelected({ note: event.target.value })}
                    placeholder="可选"
                  />
                </label>
                <div className="evaluation-selected-list">
                  {(selectedLabel.relevant_ids || []).map((memoryId) => (
                    <button
                      className="count-pill"
                      type="button"
                      key={memoryId}
                      onClick={() => toggleRelevant(memoryId)}
                      title="移除相关记忆"
                    >
                      {shortId(memoryId)}
                    </button>
                  ))}
                  {selectedLabel.relevant_ids.length === 0 && normalizedJudgment(selectedLabel) === "no_answer" && (
                    <span className="muted-line">已标记为无答案</span>
                  )}
                  {selectedLabel.relevant_ids.length === 0 && normalizedJudgment(selectedLabel) === "unlabeled" && (
                    <span className="muted-line">尚未标注</span>
                  )}
                  {selectedLabel.relevant_ids.length === 0 && normalizedJudgment(selectedLabel) === "relevant" && (
                    <span className="muted-line">尚未选择相关记忆</span>
                  )}
                </div>
              </>
            ) : (
              <EmptyBlock label="选择或新增一个 query" />
            )}
          </div>

          <div className="panel evaluation-candidates-panel">
            <div className="panel-header compact-header">
              <div>
                <h2>候选记忆</h2>
                <p className="muted-line">
                  {state.workbench.candidates.length} 条候选记忆 · 仅包含默认检索可见的普通用户事实
                </p>
              </div>
            </div>
            <div className="evaluation-candidate-list">
              {state.workbench.candidates.map((candidate) => (
                <CandidateRow
                  candidate={candidate}
                  key={candidate.id}
                  checked={Boolean(selectedLabel?.relevant_ids.includes(candidate.id))}
                  onToggle={() => toggleRelevant(candidate.id)}
                  disabled={!selectedLabel}
                />
              ))}
            </div>
          </div>
        </section>
      ) : (
        !state.loading && (
          <section className="panel">
            <EmptyBlock
              label="尚未初始化召回评测快照"
              hint="初始化会把当前真实库快照到本地评测目录，标注与运行都在隔离快照上进行。"
              action={{ label: "初始化快照", onClick: () => void initialize() }}
            />
          </section>
        )
      )}

      {state.workbench && (
        <section className="panel evaluation-run-panel">
          <div className="panel-header compact-header">
            <div>
              <h2>基线运行</h2>
              <p className="muted-line">
                运行前会自动保存当前标注；评测在隔离快照上进行，强制 record_usage=false。语义基线为 embedding+关键词混合检索。
              </p>
            </div>
            <div className="button-row end">
              <button
                className="secondary-button"
                type="button"
                onClick={save}
                disabled={saving || runningMode !== null}
              >
                <Save size={16} />
                {saving ? "保存中" : "保存标注"}
              </button>
              <button
                className="primary-button"
                type="button"
                onClick={() => void run("keyword")}
                disabled={runningMode !== null || saving}
              >
                <Play size={16} />
                {runningMode === "keyword" ? "运行中" : "关键词基线"}
              </button>
              <button
                className="secondary-button"
                type="button"
                onClick={() => void run("embedding")}
                disabled={runningMode !== null || saving}
              >
                <BarChart3 size={16} />
                {runningMode === "embedding" ? "运行中" : "语义基线"}
              </button>
            </div>
          </div>
          <div className="evaluation-results-grid">
            <ResultCard mode="keyword" result={results.keyword || null} candidateMap={candidateMap} />
            <ResultCard mode="embedding" result={results.embedding || null} candidateMap={candidateMap} />
          </div>
        </section>
      )}
      <ConfirmDialog state={confirmState} onResolve={resolveConfirm} />
    </div>
  );
}

function VerdictTile({ verdict }: { verdict: MechanismVerdict }) {
  return (
    <article className={`evaluation-verdict evaluation-verdict-${verdict.state}`}>
      <div>
        <span>{displayText(verdict.mechanism)}</span>
        <strong>{displayText(verdict.state)}</strong>
      </div>
      <p>{verdict.message}</p>
    </article>
  );
}

function CandidateRow({
  candidate,
  checked,
  disabled,
  onToggle
}: {
  candidate: MemoryRecord;
  checked: boolean;
  disabled: boolean;
  onToggle: () => void;
}) {
  return (
    <label className={`evaluation-candidate ${checked ? "selected" : ""}`}>
      <input type="checkbox" checked={checked} disabled={disabled} onChange={onToggle} />
      <span>
        <strong>{candidate.content}</strong>
        <small>
          {shortId(candidate.id)} · {displayText(candidate.type)} · 重要度 {candidate.importance}
          {candidate.redacted ? " · 已遮罩" : ""}
        </small>
      </span>
      {checked && <Check size={16} />}
    </label>
  );
}

function ResultCard({
  mode,
  result,
  candidateMap
}: {
  mode: RunMode;
  result: RecallEvalRunResult | null;
  candidateMap: Map<string, MemoryRecord>;
}) {
  return (
    <article className="evaluation-result-card">
      <div className="panel-header compact-header">
        <h3>{displayText(mode)}基线</h3>
        {result && <span className="count-pill">k={result.summary.k || 8}</span>}
      </div>
      {!result ? (
        <EmptyBlock label="暂无运行结果" compact />
      ) : (
        <>
          <div className="evaluation-metrics">
            <Metric label="Hit" value={result.summary.hit_rate} />
            <Metric label="P@k" value={result.summary.precision_at_k} />
            <Metric label="R@k" value={result.summary.recall_at_k} />
            <Metric label="MRR" value={result.summary.mrr} />
            <Metric label="nDCG" value={result.summary.ndcg_at_k} />
            {(result.summary.queries_no_answer || 0) > 0 && (
              <>
                <Metric label="无答案误召" value={result.summary.no_answer_false_positive_rate} />
                <Metric label="无答案拒答" value={result.summary.no_answer_abstention_rate} />
                <Metric label="平均误召" value={result.summary.no_answer_mean_retrieved} />
              </>
            )}
          </div>
          <div className="evaluation-query-results">
            {result.per_query.map((row) => {
              const judgment = row.judgment || (row.relevant_count > 0 ? "relevant" : "unlabeled");
              return (
                <details key={`${mode}-${row.id || row.query}`}>
                  <summary>
                    <span>{row.query}</span>
                    <em>
                      {judgment === "no_answer"
                        ? row.false_positive
                          ? `误召 ${row.retrieved} 条`
                          : "正确拒答"
                        : judgment === "unlabeled"
                          ? `未标注 · 召回 ${row.retrieved} 条`
                          : `hit=${Math.round(row.hit)} · r=${numberText(row.recall)} · ndcg=${numberText(row.ndcg)}`}
                    </em>
                  </summary>
                  <div className="evaluation-predictions">
                    <span className="muted-line">实际检索：{retrievalModeText(row.retrieval_mode)}</span>
                    {row.fallback_reason && <span className="muted-line">回退：{row.fallback_reason}</span>}
                    {row.predicted_ids.length === 0 && <span className="muted-line">无召回结果</span>}
                    {row.predicted_ids.map((memoryId) => (
                      <span
                        className="count-pill"
                        key={memoryId}
                        title={candidateMap.get(memoryId)?.content || memoryId}
                      >
                        {shortId(memoryId)}
                      </span>
                    ))}
                  </div>
                </details>
              );
            })}
          </div>
        </>
      )}
    </article>
  );
}

function Metric({ label, value }: { label: string; value?: number | null }) {
  return (
    <div>
      <span>{label}</span>
      <strong>{numberText(value)}</strong>
    </div>
  );
}
