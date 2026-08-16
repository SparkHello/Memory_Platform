import { useMemo, useState } from "react";
import { ChevronDown, RefreshCcw } from "lucide-react";
import { MemoryApi } from "../../api";
import type { DecisionLog, DecisionLogAction, ProvidersStatus } from "../../types";
import { badge } from "../../components/Badge";
import { FieldList, FilterSelect } from "../../components/FormControls";
import { PageHeader } from "../../components/PageHeader";
import { EmptyBlock, ErrorBlock, LoadingBlock } from "../../components/StateBlocks";
import { NextStepHint } from "../../components/NextStepHint";
import { Modal } from "../../components/Modal";
import { useAsyncData } from "../../hooks/useAsyncData";
import { DECISIONS } from "../../utils/constants";
import { candidateSummary, dateText, prettyJson } from "../../utils/format";

const PAGE_SIZE = 100;

export function DecisionLogsPage({
  api,
  setupStatus
}: {
  api: MemoryApi;
  setupStatus?: ProvidersStatus["setup"] | null;
}) {
  const [limit, setLimit] = useState(PAGE_SIZE);
  const [decision, setDecision] = useState<"all" | DecisionLogAction>("all");
  const [conversationId, setConversationId] = useState("");
  const [selected, setSelected] = useState<DecisionLog | null>(null);

  const { state, reload: load } = useAsyncData<DecisionLog[]>(
    (signal) => api.decisionLogs(limit, {}, signal),
    [api, limit]
  );

  const logs = useMemo(() => {
    return (state.data || []).filter((log) => {
      if (decision !== "all" && log.decision !== decision) return false;
      if (conversationId.trim()) {
        return (log.conversation_id || "").includes(conversationId.trim());
      }
      return true;
    });
  }, [conversationId, decision, state.data]);

  // 服务端只支持 limit 截断；拿满 limit 条说明后面可能还有更早的记录。
  const hasMore = (state.data?.length ?? 0) >= limit;

  return (
    <div className="page-stack">
      <PageHeader
        title="决策日志"
        subtitle="查看记忆保存、更新和忽略决策。"
        action={
          <button className="secondary-button" type="button" onClick={() => void load()}>
            <RefreshCcw size={16} />
            刷新
          </button>
        }
      />
      <section className="panel">
        <div className="toolbar log-toolbar">
          <FilterSelect
            label="决策"
            value={decision}
            options={["all", ...DECISIONS]}
            onChange={(value) => setDecision(value as "all" | DecisionLogAction)}
          />
          <label className="field-block small log-conversation-field">
            <span>对话 ID</span>
            <input
              value={conversationId}
              onChange={(event) => setConversationId(event.target.value)}
              placeholder="过滤对话 ID"
            />
          </label>
        </div>
        {state.loading && <LoadingBlock label="正在加载决策日志" />}
        {state.error && <ErrorBlock message={state.error} onRetry={() => void load()} />}
        {!state.loading && !state.error && logs.length === 0 && (
          <>
            <NextStepHint setup={setupStatus} />
            <EmptyBlock
            label="暂无决策日志"
            hint="记忆每一次被保存、更新或忽略的决策都会记录在这里。"
            action={
              decision !== "all" || conversationId.trim()
                ? {
                    label: "清除筛选",
                    onClick: () => {
                      setDecision("all");
                      setConversationId("");
                    }
                  }
                : undefined
            }
          />
          </>
        )}
        {logs.length > 0 && (
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>决策</th>
                  <th>原因</th>
                  <th>对话 ID</th>
                  <th>创建时间</th>
                  <th>候选记忆摘要</th>
                </tr>
              </thead>
              <tbody>
                {logs.map((log) => (
                  <tr
                    key={log.id}
                    tabIndex={0}
                    onClick={() => setSelected(log)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        setSelected(log);
                      }
                    }}
                  >
                    <td>{badge(log.decision)}</td>
                    <td>{log.reason}</td>
                    <td>{log.conversation_id || "-"}</td>
                    <td>{dateText(log.created_at)}</td>
                    <td className="content-cell">{candidateSummary(log.candidate_json)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {!state.loading && !state.error && hasMore && (
          <div className="button-row">
            <button className="secondary-button" type="button" onClick={() => setLimit((current) => current + PAGE_SIZE)}>
              <ChevronDown size={16} />
              加载更多
            </button>
            <span className="muted">已加载 {state.data?.length ?? 0} 条</span>
          </div>
        )}
      </section>
      {selected && (
        <Modal title="日志详情" onClose={() => setSelected(null)}>
          <FieldList
            entries={[
              ["决策", selected.decision],
              ["原因", selected.reason],
              ["对话 ID", selected.conversation_id],
              ["创建时间", dateText(selected.created_at)]
            ]}
          />
          <details className="raw-json-details">
            <summary>查看原始 JSON</summary>
            <pre className="json-block">{prettyJson(selected.candidate_json)}</pre>
          </details>
        </Modal>
      )}
    </div>
  );
}


