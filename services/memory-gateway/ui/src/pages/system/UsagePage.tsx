import {
  ArrowDownToLine,
  ArrowUpFromLine,
  Coins,
  DatabaseZap,
  RefreshCcw
} from "lucide-react";
import { useState } from "react";
import { type MemoryApi } from "../../api";
import { DataTable } from "../../components/DataTable";
import { PageHeader } from "../../components/PageHeader";
import { EmptyBlock, ErrorBlock, LoadingBlock } from "../../components/StateBlocks";
import { NextStepHint } from "../../components/NextStepHint";
import { useAsyncData } from "../../hooks/useAsyncData";
import type { CentralModelUsageSummary, ProvidersStatus } from "../../types";

type RangeKey = "7" | "30" | "90" | "all";

const RANGES: Array<{ key: RangeKey; label: string }> = [
  { key: "7", label: "7 天" },
  { key: "30", label: "30 天" },
  { key: "90", label: "90 天" },
  { key: "all", label: "全部" }
];

export function UsagePage({
  api,
  setupStatus,
  expertMode = true
}: {
  api: MemoryApi;
  setupStatus?: ProvidersStatus["setup"] | null;
  /** 简洁模式隐藏 Deployment / Attempt 等技术账本，只留结果性指标。 */
  expertMode?: boolean;
}) {
  const [range, setRange] = useState<RangeKey>("30");
  const { state, reload: load } = useAsyncData<CentralModelUsageSummary>(
    (signal) => api.modelUsage(range, signal),
    [api, range]
  );
  const summary = state.data;
  const loading = state.loading;
  const error = state.error;

  return (
    <div className="page-stack usage-page">
      <PageHeader
        title="用量与费用"
        subtitle="按 Model Gateway 实际命中的渠道、模型和上游 usage 统计每一次调用。"
        action={
          <div className="usage-header-actions">
            <div className="tabs usage-range-tabs" aria-label="统计范围">
              {RANGES.map((option) => (
                <button
                  className={range === option.key ? "active" : ""}
                  key={option.key}
                  type="button"
                  onClick={() => setRange(option.key)}
                  aria-pressed={range === option.key}
                >
                  {option.label}
                </button>
              ))}
            </div>
            <button className="secondary-button" type="button" onClick={() => void load()}>
              <RefreshCcw size={16} />
              刷新
            </button>
          </div>
        }
      />

      {loading && !summary && <LoadingBlock label="正在汇总模型用量" />}
      {error && <ErrorBlock message={error} onRetry={() => void load()} />}
      {summary && <CentralUsageView summary={summary} setupStatus={setupStatus} expertMode={expertMode} />}
    </div>
  );
}

function CentralUsageView({
  summary,
  setupStatus,
  expertMode
}: {
  summary: CentralModelUsageSummary;
  setupStatus?: ProvidersStatus["setup"] | null;
  expertMode: boolean;
}) {
  const incompleteCalls = Math.max(0, summary.calls - summary.complete_calls);
  return (
    <>
      <section className="panel usage-overview">
        <div className="usage-cost-block">
          <span>已知实际费用</span>
          <strong>{multiCurrencyCost(summary.estimated_costs)}</strong>
          <small>按每个真实上游 attempt 的价格快照汇总</small>
        </div>
        <div className="usage-ledger">
          <div>
            <Coins size={17} />
            <span><small>逻辑调用</small><strong>{tokenText(summary.calls)} 次</strong></span>
          </div>
          <div>
            <ArrowDownToLine size={17} />
            <span><small>输入 Token</small><strong>{tokenText(summary.input_tokens)}</strong></span>
          </div>
          <div>
            <ArrowUpFromLine size={17} />
            <span><small>输出 Token</small><strong>{tokenText(summary.output_tokens)}</strong></span>
          </div>
          <div>
            <DatabaseZap size={17} />
            <span><small>实际上游尝试</small><strong>{tokenText(summary.attempts.recorded)} 次</strong></span>
          </div>
        </div>
      </section>

      {summary.calls === 0 ? (
        <>
          <NextStepHint setup={setupStatus} />
          <section className="panel">
            <EmptyBlock
              label="当前用户还没有中央模型调用"
              hint="Model Gateway 按不可逆用户标签隔离统计，不保存提示词或回复正文。"
            />
          </section>
        </>
      ) : (
        <>
          <section className="usage-split">
            <section className="panel usage-operation-panel">
              <div className="panel-header compact-header"><h2>调用完整度</h2></div>
              <dl className="usage-coverage-list">
                <div><dt>完整逻辑调用</dt><dd>{summary.complete_calls} 次</dd></div>
                <div><dt>不完整或失败</dt><dd>{incompleteCalls} 次</dd></div>
                <div><dt>费用信息不完整</dt><dd>{summary.incomplete_cost_calls} 次</dd></div>
              </dl>
            </section>
            {expertMode && (
            <section className="panel usage-operation-panel">
              <div className="panel-header compact-header"><h2>逐 Attempt 账本</h2></div>
              <dl className="usage-coverage-list">
                <div><dt>已知费用</dt><dd>{summary.attempts.known_cost_attempts} 次</dd></div>
                <div><dt>可能已计费但未知</dt><dd>{summary.attempts.unknown_cost_attempts} 次</dd></div>
                <div><dt>未发往供应商</dt><dd>{summary.attempts.not_sent_attempts} 次</dd></div>
              </dl>
              <p className="usage-coverage-note">
                fallback 的每次真实请求分别记账；未知费用不会被伪装为零。
              </p>
            </section>
            )}
          </section>

          <section className="panel usage-table-panel">
            <div className="panel-header compact-header">
              <div>
                <h2>{expertMode ? "实际 Deployment" : "渠道与模型"}</h2>
                <p className="muted-line">按实际渠道与上游模型归因，不按客户端请求名猜测。</p>
              </div>
            </div>
            <DataTable>
              <thead>
                <tr>
                  <th>渠道 / 模型</th>
                  {expertMode && <th>Deployment</th>}
                  <th>调用</th>
                  <th>Token</th>
                </tr>
              </thead>
              <tbody>
                {summary.deployments.map((item) => (
                  <tr key={`${item.deployment_id}:${item.connection_id}`}>
                    <td>
                      <div className="usage-model-cell">
                        <span>{item.channel_operator || "unknown"}</span>
                        <code>{item.upstream_model || "unknown"}</code>
                        <small>作者：{item.model_author || "unknown"}</small>
                      </div>
                    </td>
                    {expertMode && <td><code>{item.deployment_id}</code></td>}
                    <td>{tokenText(item.calls)}</td>
                    <td>{tokenText(item.total_tokens)}</td>
                  </tr>
                ))}
              </tbody>
            </DataTable>
          </section>
        </>
      )}

      <section className="panel panel--quiet usage-coverage-note" aria-label="账本保留策略">
        原始逐请求与逐 attempt 事件保留 {summary.retention.raw_days} 天；日聚合保留 {summary.retention.daily_days} 天。
        当前视图统计最近 {summary.days} 天。
      </section>
    </>
  );
}

function tokenText(value: number) {
  return Number(value || 0).toLocaleString("zh-CN");
}

function formatCny(value: number) {
  const amount = Number(value || 0);
  const digits = amount > 0 && amount < 0.01 ? 6 : amount < 1 ? 4 : 2;
  return new Intl.NumberFormat("zh-CN", {
    style: "currency",
    currency: "CNY",
    minimumFractionDigits: digits,
    maximumFractionDigits: digits
  }).format(amount);
}

function multiCurrencyCost(costs: Record<string, string>) {
  const values = Object.entries(costs).filter(([, raw]) => Number.isFinite(Number(raw)));
  if (values.length === 0) return "—";
  return values
    .map(([currency, raw]) => {
      const value = Number(raw);
      if (currency === "CNY") return formatCny(value);
      return `${currency} ${value.toLocaleString("zh-CN", { maximumFractionDigits: 6 })}`;
    })
    .join(" · ");
}
