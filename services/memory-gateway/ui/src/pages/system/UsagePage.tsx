import {
  ArrowDownToLine,
  ArrowUpFromLine,
  Coins,
  DatabaseZap,
  RefreshCcw
} from "lucide-react";
import { useMemo, useState } from "react";
import { type MemoryApi } from "../../api";
import { DataTable } from "../../components/DataTable";
import { PageHeader } from "../../components/PageHeader";
import { EmptyBlock, ErrorBlock, LoadingBlock } from "../../components/StateBlocks";
import { useAsyncData } from "../../hooks/useAsyncData";
import type {
  CentralModelUsageSummary,
  ModelUsageSummary,
  UsageEvent,
  UsageModelBreakdown,
  UsageTotals
} from "../../types";
import { dateText, percent } from "../../utils/format";

type RangeKey = "7" | "30" | "90" | "all";

const RANGES: Array<{ key: RangeKey; label: string }> = [
  { key: "7", label: "7 天" },
  { key: "30", label: "30 天" },
  { key: "90", label: "90 天" },
  { key: "all", label: "全部" }
];

const OPERATION_LABELS: Record<string, string> = {
  chat_completion: "聊天回复",
  "memory-extractor": "单条记忆提取",
  "memory-ingester": "记忆提取",
  "memory-context-compactor": "上下文压缩",
  "core-memory-consolidator": "核心记忆整理",
  "memory-review-editor": "体检 AI 修改",
  memory_search: "记忆语义召回",
  memory_write: "记忆写入向量化",
  memory_reembed: "记忆重新向量化",
  knowledge_search: "知识语义检索",
  knowledge_index: "知识索引向量化",
  knowledge_agent_flash: "知识代理 · 快速",
  knowledge_agent_pro: "知识代理 · 升级"
};

export function UsagePage({ api }: { api: MemoryApi }) {
  const [range, setRange] = useState<RangeKey>("30");
  const { state, reload: load } = useAsyncData<ModelUsageSummary>(
    (signal) => api.modelUsage(range, signal),
    [api, range]
  );
  const summary = state.data;
  const loading = state.loading;
  const error = state.error;

  const localSummary = summary && "totals" in summary ? summary : null;
  const centralSummary = summary && "attempts" in summary ? summary : null;

  const visibleDaily = useMemo(
    () => (localSummary?.daily || []).slice(range === "7" ? -7 : -30),
    [range, localSummary?.daily]
  );
  const maxDailyTokens = Math.max(
    1,
    ...visibleDaily.map((day) => day.input_tokens + day.output_tokens)
  );

  return (
    <div className="page-stack usage-page">
      <PageHeader
        title="用量与费用"
        subtitle="按实际命中的 provider、模型和上游 usage 统计每一次模型调用。"
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

      {centralSummary && <CentralUsageView summary={centralSummary} />}

      {localSummary && (
        <>
          <UsageOverview totals={localSummary.totals} />

          {localSummary.totals.calls === 0 ? (
            <section className="panel">
              <EmptyBlock
                label="还没有可统计的调用"
                hint="升级后的新调用会自动记录；历史账单不会反向估算。"
              />
            </section>
          ) : (
            <>
              <section className="panel usage-chart-panel">
                <div className="panel-header compact-header">
                  <div>
                    <h2>Token 走势</h2>
                    <p className="muted-line">
                      {range === "all" || range === "90"
                        ? "显示最近 30 个有调用的日期"
                        : "按实际发生调用的日期汇总"}
                    </p>
                  </div>
                  <div className="usage-chart-legend" aria-label="图例">
                    <span><i className="input" />输入</span>
                    <span><i className="output" />输出</span>
                  </div>
                </div>
                <table className="sr-only">
                  <caption>每日输入、输出 Token 与可计费金额</caption>
                  <thead>
                    <tr>
                      <th scope="col">日期</th>
                      <th scope="col">输入 Token</th>
                      <th scope="col">输出 Token</th>
                      <th scope="col">可计费金额</th>
                    </tr>
                  </thead>
                  <tbody>
                    {visibleDaily.map((day) => (
                      <tr key={day.date}>
                        <th scope="row">{day.date}</th>
                        <td>{tokenText(day.input_tokens)}</td>
                        <td>{tokenText(day.output_tokens)}</td>
                        <td>{coverageAmountText(day)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <div className="usage-chart" aria-hidden="true">
                  {visibleDaily.map((day) => {
                    const inputHeight = day.input_tokens
                      ? Math.max(2, (day.input_tokens / maxDailyTokens) * 100)
                      : 0;
                    const outputHeight = day.output_tokens
                      ? Math.max(2, (day.output_tokens / maxDailyTokens) * 100)
                      : 0;
                    return (
                      <div
                        className="usage-day"
                        key={day.date}
                        title={`${day.date} · 输入 ${tokenText(day.input_tokens)} · 输出 ${tokenText(day.output_tokens)} · ${coverageAmountText(day)}`}
                      >
                        <div className="usage-bar">
                          <span className="usage-bar-output" style={{ height: `${outputHeight}%` }} />
                          <span className="usage-bar-input" style={{ height: `${inputHeight}%` }} />
                        </div>
                        <time dateTime={day.date}>{shortDate(day.date)}</time>
                      </div>
                    );
                  })}
                </div>
              </section>

              <section className="panel usage-table-panel">
                <div className="panel-header compact-header">
                  <div>
                    <h2>实际模型</h2>
                    <p className="muted-line">故障切换后的最终 provider 会单独归账。</p>
                  </div>
                </div>
                <DataTable>
                  <thead>
                    <tr>
                      <th>Provider / 模型</th>
                      <th>调用</th>
                      <th>输入 Token</th>
                      <th>输出 Token</th>
                      <th>缓存命中</th>
                      <th>金额</th>
                      <th>覆盖</th>
                    </tr>
                  </thead>
                  <tbody>
                    {localSummary.by_model.map((item) => (
                      <ModelRow item={item} key={`${item.provider}:${item.model}:${item.kind}`} />
                    ))}
                  </tbody>
                </DataTable>
              </section>

              <section className="usage-split">
                <section className="panel usage-operation-panel">
                  <div className="panel-header compact-header">
                    <h2>调用用途</h2>
                  </div>
                  <div className="usage-operation-list">
                    {localSummary.by_operation.map((item) => (
                      <div key={item.operation}>
                        <span>
                          <strong>{operationLabel(item.operation)}</strong>
                          <small>{item.calls} 次 · {tokenText(item.total_tokens)} Token</small>
                        </span>
                        <b>{coverageAmountText(item)}</b>
                      </div>
                    ))}
                  </div>
                </section>

                <section className="panel usage-operation-panel">
                  <div className="panel-header compact-header">
                    <h2>计费完整度</h2>
                  </div>
                  <dl className="usage-coverage-list">
                    <div>
                      <dt>已精确计费</dt>
                      <dd>{localSummary.totals.priced_calls} 次</dd>
                    </div>
                    <div>
                      <dt>上游未返回 usage</dt>
                      <dd>{localSummary.totals.unmeasured_calls} 次</dd>
                    </div>
                    <div>
                      <dt>暂无价格映射</dt>
                      <dd>{localSummary.totals.unpriced_calls} 次</dd>
                    </div>
                  </dl>
                  <p className="usage-coverage-note">
                    总金额只累加同时具备上游 usage 与官方单价的调用。
                  </p>
                </section>
              </section>

              <section className="panel usage-table-panel">
                <div className="panel-header compact-header">
                  <div>
                    <h2>最近调用</h2>
                    <p className="muted-line">只保存计量元数据，不保存提示词或回复正文。</p>
                  </div>
                </div>
                <DataTable>
                  <thead>
                    <tr>
                      <th>时间</th>
                      <th>用途</th>
                      <th>模型</th>
                      <th>Token</th>
                      <th>金额</th>
                      <th>状态</th>
                    </tr>
                  </thead>
                  <tbody>
                    {localSummary.recent.map((event) => (
                      <RecentRow event={event} key={event.id} />
                    ))}
                  </tbody>
                </DataTable>
              </section>
            </>
          )}

          <details className="panel usage-pricing">
            <summary>
              <span>
                <strong>当前价格表</strong>
                <small>官方公开 API 原价 · 截至 {localSummary.pricing.as_of}</small>
              </span>
              <span>{localSummary.pricing.models.length} 个价格项</span>
            </summary>
            <div className="usage-price-list">
              {localSummary.pricing.models.map((price) => (
                <div key={price.key}>
                  <span>
                    <strong>{price.provider_label}</strong>
                    <code>{price.model}</code>
                    {price.input_range_label && <small>{price.input_range_label}</small>}
                  </span>
                  <span className="usage-price-values">
                    {price.kind === "embedding" ? (
                      <>输入 ¥{price.input_cache_miss_per_million} / MTok</>
                    ) : (
                      <>
                        缓存 ¥{price.input_cache_hit_per_million} · 输入 ¥{price.input_cache_miss_per_million} · 输出 ¥{price.output_per_million} / MTok
                      </>
                    )}
                  </span>
                  <a href={price.source_url} target="_blank" rel="noreferrer">官方来源</a>
                </div>
              ))}
            </div>
            <p className="usage-pricing-note">{localSummary.pricing.note}</p>
          </details>
        </>
      )}
    </div>
  );
}

function CentralUsageView({ summary }: { summary: CentralModelUsageSummary }) {
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
        <section className="panel">
          <EmptyBlock
            label="当前用户还没有中央模型调用"
            hint="Model Gateway 按不可逆用户标签隔离统计，不保存提示词或回复正文。"
          />
        </section>
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
          </section>

          <section className="panel usage-table-panel">
            <div className="panel-header compact-header">
              <div>
                <h2>实际 Deployment</h2>
                <p className="muted-line">按实际渠道与上游模型归因，不按客户端请求名猜测。</p>
              </div>
            </div>
            <DataTable>
              <thead>
                <tr>
                  <th>渠道 / 模型</th>
                  <th>Deployment</th>
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
                    <td><code>{item.deployment_id}</code></td>
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

function UsageOverview({ totals }: { totals: UsageTotals }) {
  return (
    <section className="panel usage-overview">
      <div className="usage-cost-block">
        <span>可计费金额</span>
        <strong>{formatCny(totals.cost_cny)}</strong>
        <small>人民币 · 官方公开 API 原价</small>
      </div>
      <div className="usage-ledger">
        <div>
          <Coins size={17} />
          <span><small>模型调用</small><strong>{tokenText(totals.calls)} 次</strong></span>
        </div>
        <div>
          <ArrowDownToLine size={17} />
          <span><small>输入 Token</small><strong>{tokenText(totals.input_tokens)}</strong></span>
        </div>
        <div>
          <ArrowUpFromLine size={17} />
          <span><small>输出 Token</small><strong>{tokenText(totals.output_tokens)}</strong></span>
        </div>
        <div>
          <DatabaseZap size={17} />
          <span>
            <small>缓存命中</small>
            <strong>{tokenText(totals.cached_input_tokens)} · {percent(totals.cache_hit_rate)}</strong>
          </span>
        </div>
      </div>
    </section>
  );
}

function ModelRow({ item }: { item: UsageModelBreakdown }) {
  return (
    <tr>
      <td>
        <div className="usage-model-cell">
          <span>{item.provider_label}</span>
          <code>{item.model}</code>
          <small>{item.kind === "embedding" ? "Embedding" : "Chat"}</small>
        </div>
      </td>
      <td>{tokenText(item.calls)}</td>
      <td>{tokenText(item.input_tokens)}</td>
      <td>{tokenText(item.output_tokens)}</td>
      <td>{tokenText(item.cached_input_tokens)} <small className="usage-table-sub">{percent(item.cache_hit_rate)}</small></td>
      <td className="usage-money-cell">{coverageAmountText(item)}</td>
      <td>{coverageBadge(item)}</td>
    </tr>
  );
}

function RecentRow({ event }: { event: UsageEvent }) {
  const status = !event.usage_available
    ? { label: "缺少 usage", tone: "warning" }
    : !event.price_available
      ? { label: "待定价", tone: "muted" }
      : { label: "精确", tone: "success" };
  return (
    <tr>
      <td className="usage-time-cell">{dateText(event.created_at)}</td>
      <td>{operationLabel(event.operation)}</td>
      <td>
        <div className="usage-model-cell compact-model">
          <span>{event.provider_label}</span>
          <code>{event.model}</code>
        </div>
      </td>
      <td>{event.usage_available ? tokenText(event.total_tokens || 0) : "—"}</td>
      <td className="usage-money-cell">
        {event.cost_cny === null || event.cost_cny === undefined
          ? "—"
          : formatCny(event.cost_cny)}
      </td>
      <td><span className={`usage-status usage-status-${status.tone}`}>{status.label}</span></td>
    </tr>
  );
}

function coverageBadge(item: UsageTotals) {
  if (item.unmeasured_calls > 0) {
    return <span className="usage-status usage-status-warning">{item.unmeasured_calls} 次缺 usage</span>;
  }
  if (item.unpriced_calls > 0) {
    return <span className="usage-status usage-status-muted">{item.unpriced_calls} 次待定价</span>;
  }
  return <span className="usage-status usage-status-success">完整</span>;
}

function operationLabel(operation: string) {
  return OPERATION_LABELS[operation] || operation.replace(/_/g, " ");
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

function coverageAmountText(item: UsageTotals) {
  if (item.priced_calls === 0) return "—";
  const amount = formatCny(item.cost_cny);
  return item.priced_calls < item.calls ? `${amount}+` : amount;
}

function shortDate(value: string) {
  const date = new Date(`${value}T00:00:00`);
  if (Number.isNaN(date.getTime())) return value.slice(5);
  return date.toLocaleDateString("zh-CN", { month: "numeric", day: "numeric" });
}
