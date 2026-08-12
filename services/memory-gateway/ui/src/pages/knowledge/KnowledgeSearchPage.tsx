import {
  Bot,
  Check,
  ChevronDown,
  Clipboard,
  Clock3,
  Filter,
  ListTree,
  RefreshCcw,
  Search,
  ShieldAlert,
  Sparkles,
  X
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { isAbortError, type MemoryApi } from "../../api";
import { PageHeader } from "../../components/PageHeader";
import { DataTable } from "../../components/DataTable";
import { EmptyBlock, ErrorBlock, LoadingBlock } from "../../components/StateBlocks";
import type {
  KnowledgeAgentStep,
  KnowledgeDocument,
  KnowledgeSearchHit,
  KnowledgeSearchQuality,
  KnowledgeSearchResponse,
  KnowledgeStatus
} from "../../types";
import { copyText } from "../../utils/files";
import { errorMessage, numberText } from "../../utils/format";
import type { Notify } from "../pageTypes";
import { knowledgeDocumentRef } from "./knowledgeData";

export function KnowledgeSearchPage({
  api,
  notify,
  onOpenDocument,
  status
}: {
  api: MemoryApi;
  notify: Notify;
  onOpenDocument: (id: string) => void;
  status: KnowledgeStatus | null;
}) {
  const [documents, setDocuments] = useState<KnowledgeDocument[]>([]);
  const [documentsError, setDocumentsError] = useState<string | null>(null);
  const [request, setRequest] = useState("");
  const [quality, setQuality] = useState<KnowledgeSearchQuality>("balanced");
  const [limit, setLimit] = useState(5);
  const [includeSensitive, setIncludeSensitive] = useState(false);
  const [tagsText, setTagsText] = useState("");
  const [metadataText, setMetadataText] = useState("");
  const [selectedRefs, setSelectedRefs] = useState<Set<string>>(() => new Set());
  const [result, setResult] = useState<KnowledgeSearchResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const loadDocuments = useCallback(async (signal?: AbortSignal) => {
    setDocumentsError(null);
    try {
      setDocuments(await api.listKnowledgeDocuments({ status: "active", limit: 500 }, signal));
    } catch (loadError) {
      if (isAbortError(loadError)) return;
      setDocumentsError(errorMessage(loadError));
    }
  }, [api]);

  useEffect(() => {
    const controller = new AbortController();
    void loadDocuments(controller.signal);
    return () => controller.abort();
  }, [loadDocuments]);

  // 前端超时跟随后端 KNOWLEDGE_AGENT_TIMEOUT_SECONDS（留 10s 余量）；status 未加载时回退 35s。
  const searchTimeoutMs = status?.agent_timeout_seconds ? status.agent_timeout_seconds * 1000 + 10000 : 35000;
  const egressWarning = Boolean(status?.agent_enabled && status.agent_egress_policy && status.agent_egress_policy !== "none");
  const providerSummary = (status?.agent_configured_providers || [])
    .map((provider) => PROVIDER_LABELS[provider] || provider)
    .join(" → ");

  const runSearch = async () => {
    const cleanRequest = request.trim();
    if (!cleanRequest) {
      notify("请先描述要查找的内容", "error");
      return;
    }
    setLoading(true);
    setError(null);
    try {
      setResult(await api.searchKnowledge({
        request: cleanRequest,
        limit,
        documentRefs: [...selectedRefs],
        tags: parseTags(tagsText),
        metadataFilter: parseMetadataFilter(metadataText),
        quality,
        includeSensitive,
        timeoutMs: searchTimeoutMs
      }));
    } catch (searchError) {
      setResult(null);
      setError(errorMessage(searchError));
    } finally {
      setLoading(false);
    }
  };

  const toggleDocument = (reference: string) => {
    if (!selectedRefs.has(reference) && selectedRefs.size >= 50) {
      notify("一次最多限定 50 个文档", "error");
      return;
    }
    setSelectedRefs((current) => {
      const next = new Set(current);
      if (next.has(reference)) next.delete(reference);
      else next.add(reference);
      return next;
    });
  };

  const hits = useMemo(() => resultHits(result), [result]);
  const localCandidates = result?.local_candidates || [];
  const documentByRef = useMemo(() => new Map(documents.map((document) => [knowledgeDocumentRef(document), document])), [documents]);
  const metadata = result?.metadata || result?.agent;
  const agentUsed = metadata?.agent_used ?? result?.agent_used ?? false;
  const model = metadata?.model || result?.agent_model || result?.model || "本地索引";
  const rounds = metadata?.rounds ?? result?.agent_rounds ?? result?.rounds ?? 0;
  const steps = metadata?.tool_steps || result?.tool_steps || result?.steps || [];
  const fallbackReason = metadata?.fallback_reason || result?.fallback_reason;
  const upgraded = metadata?.escalated ?? result?.escalated ?? result?.upgraded ?? false;
  const elapsedMs = metadata?.elapsed_ms ?? result?.elapsed_ms;
  const baselineCount = metadata?.baseline_count ?? result?.baseline_count;

  return (
    <div className="page-stack knowledge-search-page">
      <PageHeader
        title="检索调试"
        subtitle="模拟 AI 通过 MCP 描述需求，检查搜索代理如何选择逐字片段和稳定引用。"
        action={
          <button className="secondary-button" type="button" onClick={() => void loadDocuments()}>
            <RefreshCcw size={16} />刷新文档范围
          </button>
        }
      />

      <section className="panel knowledge-query-panel">
        <form onSubmit={(event) => { event.preventDefault(); void runSearch(); }}>
          <label className="field-block knowledge-request-field">
            <span>AI 的检索需求</span>
            <textarea
              value={request}
              rows={5}
              maxLength={8000}
              onChange={(event) => setRequest(event.target.value)}
              placeholder="例如：找到架构文档里关于敏感数据出站的约束，返回原文和对应行号。"
              data-autofocus
            />
          </label>

          <div className="knowledge-search-controls">
            <fieldset className="knowledge-quality-field">
              <legend>搜索强度</legend>
              <div className="tabs" role="radiogroup" aria-label="搜索强度">
                {(["fast", "balanced", "deep"] as KnowledgeSearchQuality[]).map((value) => (
                  <button
                    key={value}
                    type="button"
                    role="radio"
                    aria-checked={quality === value}
                    className={quality === value ? "active" : ""}
                    onClick={() => setQuality(value)}
                  >
                    {qualityLabel(value)}
                  </button>
                ))}
              </div>
            </fieldset>
            <label className="field-block knowledge-limit-field">
              <span>返回片段</span>
              <select value={limit} onChange={(event) => setLimit(Number(event.target.value))}>
                {[3, 5, 8, 10].map((value) => <option key={value} value={value}>{value} 条</option>)}
              </select>
            </label>
            <label className="checkbox-row knowledge-sensitive-toggle">
              <input type="checkbox" checked={includeSensitive} onChange={(event) => setIncludeSensitive(event.target.checked)} />
              允许检索私密/敏感文档
            </label>
            <button className="primary-button knowledge-run-search" type="submit" disabled={loading || !request.trim()}>
              <Search size={16} />{loading ? "正在检索" : "运行检索"}
            </button>
          </div>

          <details className="knowledge-scope-details">
            <summary>
              <Filter size={15} />
              <span>{selectedRefs.size ? `限定 ${selectedRefs.size} 个文档` : "搜索全部有效文档"}</span>
              <ChevronDown size={15} />
            </summary>
            <div className="knowledge-scope-toolbar">
              <span className="muted">不选择时搜索当前用户的全部有效文档。</span>
              {selectedRefs.size > 0 && <button className="ghost-button compact" type="button" onClick={() => setSelectedRefs(new Set())}><X size={13} />清除范围</button>}
            </div>
            <div className="knowledge-filter-grid">
              <label className="field-block">
                <span>标签（全部匹配）</span>
                <input value={tagsText} onChange={(event) => setTagsText(event.target.value)} placeholder="产品, 架构" />
              </label>
              <label className="field-block">
                <span>元数据精确过滤（JSON）</span>
                <input value={metadataText} onChange={(event) => setMetadataText(event.target.value)} placeholder={'{"department":"研发"}'} />
              </label>
            </div>
            {documentsError && <ErrorBlock message={documentsError} onRetry={() => void loadDocuments()} />}
            {!documentsError && documents.length === 0 && <EmptyBlock compact label="知识库暂无有效文档" />}
            {!documentsError && documents.length > 0 && (
              <div className="knowledge-scope-list">
                {documents.map((document) => {
                  const reference = knowledgeDocumentRef(document);
                  const checked = selectedRefs.has(reference);
                  return (
                    <label key={document.id} className={checked ? "selected" : ""}>
                      <input type="checkbox" checked={checked} onChange={() => toggleDocument(reference)} />
                      <span><strong>{document.title}</strong><small>{document.source_name || reference}</small></span>
                      {checked && <Check size={15} aria-hidden="true" />}
                    </label>
                  );
                })}
              </div>
            )}
          </details>
        </form>
      </section>

      {egressWarning && (
        <div className="notice warning knowledge-egress-notice">
          <ShieldAlert size={18} />
          <span>
            远程知识代理已启用{providerSummary ? `（${providerSummary}）` : ""}。检索需求和获准的候选正文可能按优先级发送给相应服务商。
            {status?.agent_rate_limit_cooldown_seconds
              ? ` 某个服务商返回 429 后，当前进程会暂时跳过它至少 ${numberText(status.agent_rate_limit_cooldown_seconds)} 秒。`
              : ""}
            请根据实际启用的服务商确认数据处理条款。
          </span>
        </div>
      )}

      {loading && <LoadingBlock label="正在运行本地基线与受限搜索代理" />}
      {error && <ErrorBlock message={error} onRetry={() => void runSearch()} />}
      {!loading && !error && !result && (
        <EmptyBlock label="等待一次检索" hint="输入自然语言需求后，结果会显示逐字片段、稳定引用、代理步骤摘要和 MCP JSON。" />
      )}

      {!loading && result && (
        <>
          <section className="knowledge-agent-summary" aria-label="检索执行摘要">
            <div><Bot size={17} /><span>执行方式</span><strong>{agentUsed ? model : "本地索引回退"}</strong></div>
            <div><ListTree size={17} /><span>工具轮次</span><strong>{rounds || steps.length}</strong></div>
            <div><Sparkles size={17} /><span>模型升级</span><strong>{upgraded ? "已升级 Pro" : "未升级"}</strong></div>
            <div><Clock3 size={17} /><span>耗时</span><strong>{elapsedMs === undefined ? "-" : `${numberText(elapsedMs)} ms`}</strong></div>
          </section>

          {fallbackReason && (
            <div className="notice warning"><ShieldAlert size={17} /><span>代理未采用，已返回本地基线：{fallbackReason}</span></div>
          )}

          {(result.query_plan?.length || steps.length) && (
            <section className="panel knowledge-steps-panel">
              <div className="panel-header"><div><h2>查询条件与工具步骤</h2><p className="muted">仅显示可审计的工具参数摘要，不展示模型思维链。</p></div></div>
              {result.query_plan && result.query_plan.length > 0 && (
                <div className="knowledge-query-plan">
                  {result.query_plan.map((query, index) => <code key={`${query}-${index}`}>{query}</code>)}
                </div>
              )}
              {steps.length > 0 && (
                <ol className="knowledge-step-list">
                  {steps.map((step, index) => <AgentStepView key={index} step={step} index={index} />)}
                </ol>
              )}
            </section>
          )}

          <section className="panel knowledge-steps-panel" aria-label="本地索引候选">
            <div className="panel-header">
              <div>
                <h2>本地候选</h2>
                <p className="muted">代理选择前的本地索引结果，仅显示定位与匹配信号；逐字正文仍以下方精确片段为准。</p>
              </div>
              <span className="result-count">{localCandidates.length} 条</span>
            </div>
            {localCandidates.length === 0 ? (
              <EmptyBlock compact label="本地索引没有候选" />
            ) : (
              <DataTable>
                <thead>
                  <tr>
                    <th>文档</th>
                    <th>版本 / 位置</th>
                    <th>分数</th>
                    <th>匹配原因</th>
                  </tr>
                </thead>
                <tbody>
                  {localCandidates.map((candidate, index) => {
                    const sourceDocument = documentByRef.get(candidate.document_ref);
                    return (
                      <tr key={`${candidate.chunk_ref}-${index}`}>
                        <td>
                          {sourceDocument ? (
                            <button className="knowledge-document-link" type="button" onClick={() => onOpenDocument(sourceDocument.id)}>
                              <strong>{candidate.title || sourceDocument.title}</strong>
                              <span>{headingText(candidate)}</span>
                            </button>
                          ) : (
                            <div>
                              <strong>{candidate.title || "知识文档"}</strong>
                              <div className="muted">{headingText(candidate)}</div>
                            </div>
                          )}
                        </td>
                        <td>
                          <code title={candidate.version_ref}>{referenceLabel(candidate.version_ref)}</code>
                          <div className="muted">{lineText(candidate) || "位置未提供"}</div>
                        </td>
                        <td>{candidate.score === undefined ? "—" : numberText(candidate.score)}</td>
                        <td>{matchSignals(candidate)}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </DataTable>
            )}
          </section>

          <section className="panel knowledge-results-panel">
            <div className="panel-header">
              <div><h2>精确片段</h2><p className="muted">正文由网关按引用逐字返回，不采纳模型生成的转述。</p></div>
              <span className="result-count">{baselineCount === undefined ? `${hits.length} 条` : `本地候选 ${baselineCount} · 返回 ${hits.length}`}</span>
            </div>
            {hits.length === 0 ? (
              <EmptyBlock label="没有可靠命中" hint="本地索引和搜索代理都没有找到满足需求的片段。" />
            ) : (
              <div className="knowledge-hit-list">
                {hits.map((hit, index) => {
                  const sourceDocument = documentByRef.get(hit.document_ref);
                  return (
                    <article className="knowledge-hit" key={`${hit.chunk_ref}-${index}`}>
                      <header>
                        <div>
                          <span className="knowledge-hit-rank">{index + 1}</span>
                          <div>
                            {sourceDocument ? <button type="button" onClick={() => onOpenDocument(sourceDocument.id)}>{hit.title || sourceDocument.title}</button> : <strong>{hit.title || "知识片段"}</strong>}
                            <small>{headingText(hit)}{lineText(hit) ? ` · ${lineText(hit)}` : ""}</small>
                          </div>
                        </div>
                        <button className="secondary-button compact" type="button" onClick={() => void copyText(hit.chunk_ref).then(() => notify("片段引用已复制", "success")).catch((cause) => notify(`复制失败：${errorMessage(cause)}`, "error"))}><Clipboard size={13} />复制引用</button>
                      </header>
                      <pre>{hit.excerpt}</pre>
                      <footer>
                        <code>{hit.chunk_ref}</code>
                        <span>{matchSignals(hit)}</span>
                        {hit.score !== undefined && <span>分数 {numberText(hit.score)}</span>}
                      </footer>
                    </article>
                  );
                })}
              </div>
            )}
          </section>

          <section className="panel knowledge-json-panel">
            <div className="panel-header">
              <div><h2>MCP 响应预览</h2><p className="muted">REST 调试结果与 MCP 搜索响应使用同一语义字段。</p></div>
              <button className="secondary-button" type="button" onClick={() => void copyText(JSON.stringify(result, null, 2)).then(() => notify("JSON 已复制", "success")).catch((cause) => notify(`复制失败：${errorMessage(cause)}`, "error"))}><Clipboard size={15} />复制 JSON</button>
            </div>
            <pre className="json-block">{JSON.stringify(result, null, 2)}</pre>
          </section>
        </>
      )}
    </div>
  );
}

const PROVIDER_LABELS: Record<string, string> = {
  M: "MiMo",
  K: "Kimi",
  D: "DeepSeek"
};

function parseTags(value: string): string[] {
  return [...new Set(value.split(/[,，]/).map((tag) => tag.trim()).filter(Boolean))];
}

function parseMetadataFilter(value: string): Record<string, string | number | boolean> {
  if (!value.trim()) return {};
  const parsed = JSON.parse(value) as unknown;
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("元数据过滤必须是 JSON 对象");
  }
  return parsed as Record<string, string | number | boolean>;
}

function AgentStepView({ step, index }: { step: KnowledgeAgentStep; index: number }) {
  const name = step.tool || step.action || `步骤 ${index + 1}`;
  const detail = step.query || step.summary || "已执行只读检索工具";
  const resultCount = step.result_count ?? step.reference_count;
  return (
    <li>
      <span>{index + 1}</span>
      <div><strong>{name}</strong><p>{detail}</p></div>
      {resultCount !== undefined && <em>{resultCount} 个引用</em>}
    </li>
  );
}

function resultHits(result: KnowledgeSearchResponse | null): KnowledgeSearchHit[] {
  return result?.results || result?.data || [];
}

function qualityLabel(value: KnowledgeSearchQuality): string {
  if (value === "fast") return "快速";
  if (value === "deep") return "深入";
  return "均衡";
}

function headingText(hit: KnowledgeSearchHit): string {
  const heading = hit.heading_path || hit.title_path;
  if (Array.isArray(heading)) return heading.join(" / ");
  return heading || hit.source_name || "正文";
}

function lineText(hit: KnowledgeSearchHit): string {
  const start = hit.line_start ?? hit.start_line;
  const end = hit.line_end ?? hit.end_line;
  if (start === undefined) return "";
  return end !== undefined && end !== start ? `第 ${start}–${end} 行` : `第 ${start} 行`;
}

function matchSignals(hit: KnowledgeSearchHit): string {
  const signals = hit.match_signals || hit.channels || [];
  return hit.match_reason || (signals.length ? signals.join(" · ") : "本地索引命中");
}

function referenceLabel(reference: string): string {
  const tail = reference.split("/").pop() || reference;
  return tail.length > 14 ? `${tail.slice(0, 6)}…${tail.slice(-5)}` : tail;
}
