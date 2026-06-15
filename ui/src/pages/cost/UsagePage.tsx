import { useCallback, useEffect, useState } from "react";
import { RefreshCcw, Save } from "lucide-react";
import { MemoryApi } from "../../api";
import type { UsageEvent, UsageSummary } from "../../types";
import { badge } from "../../components/Badge";
import { PageHeader } from "../../components/PageHeader";
import { StatCard } from "../../components/StatCard";
import { EmptyBlock, ErrorBlock, LoadingBlock } from "../../components/StateBlocks";
import type { LoadState } from "../../hooks/useAsyncData";
import { dateText, errorMessage, moneyText, numberText } from "../../utils/format";
import type { Notify } from "../pageTypes";

export function UsagePage({ api }: { api: MemoryApi }) {
  const [state, setState] = useState<LoadState<{ events: UsageEvent[]; summary: UsageSummary[] }>>({
    loading: true,
    error: null,
    data: null
  });

  const load = useCallback(async () => {
    setState({ loading: true, error: null, data: null });
    try {
      const [events, summary] = await Promise.all([api.usage(100), api.usageSummary()]);
      setState({ loading: false, error: null, data: { events, summary } });
    } catch (error) {
      setState({ loading: false, error: errorMessage(error), data: null });
    }
  }, [api]);

  useEffect(() => {
    void load();
  }, [load]);

  const data = state.data;
  const totalCalls = data?.summary.reduce((sum, item) => sum + Number(item.calls || 0), 0) || 0;
  const totalTokens = data?.summary.reduce((sum, item) => sum + Number(item.total_tokens || 0), 0) || 0;
  const totalCost = data?.summary.reduce((sum, item) => sum + Number(item.total_cost || 0), 0) || 0;
  const currency = data?.summary[0]?.currency || "CNY";

  return (
    <div className="page-stack">
      <PageHeader
        title="用量统计"
        subtitle="服务商调用记录和按服务商/模型聚合的用量。"
        action={
          <button className="secondary-button" type="button" onClick={load}>
            <RefreshCcw size={16} />
            刷新
          </button>
        }
      />
      {state.loading && <LoadingBlock label="正在加载用量" />}
      {state.error && <ErrorBlock message={state.error} onRetry={load} />}
      {data && (
        <>
          <div className="stats-grid">
            <StatCard label="调用数" value={totalCalls} />
            <StatCard label="总 Tokens" value={numberText(totalTokens)} />
            <StatCard label="费用" value={moneyText(totalCost, currency)} />
            <StatCard label="最近记录" value={data.events.length} />
          </div>

          <section className="panel">
            <div className="panel-header">
              <h2>用量汇总</h2>
            </div>
            {data.summary.length === 0 && <EmptyBlock label="暂无用量汇总" />}
            {data.summary.length > 0 && (
              <div className="table-wrap">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>服务商</th>
                      <th>虚拟模型</th>
                      <th>调用数</th>
                      <th>输入 Tokens</th>
                      <th>输出 Tokens</th>
                      <th>总 Tokens</th>
                      <th>总费用</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.summary.map((item) => (
                      <tr key={`${item.provider}-${item.virtual_model}-${item.currency}`}>
                        <td>{item.provider}</td>
                        <td>{item.virtual_model}</td>
                        <td>{numberText(item.calls)}</td>
                        <td>{numberText(item.prompt_tokens)}</td>
                        <td>{numberText(item.completion_tokens)}</td>
                        <td>{numberText(item.total_tokens)}</td>
                        <td>{moneyText(item.total_cost, item.currency)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          <section className="panel">
            <div className="panel-header">
              <h2>最近调用</h2>
            </div>
            {data.events.length === 0 && <EmptyBlock label="暂无调用记录" />}
            {data.events.length > 0 && (
              <div className="table-wrap">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>时间</th>
                      <th>状态</th>
                      <th>服务商</th>
                      <th>虚拟模型</th>
                      <th>上游模型</th>
                      <th>Tokens</th>
                      <th>费用</th>
                      <th>估算</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.events.map((event) => (
                      <tr key={event.id}>
                        <td>{dateText(event.created_at)}</td>
                        <td>{badge(event.status)}</td>
                        <td>{event.provider}</td>
                        <td>{event.virtual_model}</td>
                        <td>{event.upstream_model}</td>
                        <td>{numberText(event.total_tokens)}</td>
                        <td>{moneyText(event.total_cost, event.currency)}</td>
                        <td>{event.estimated ? "是" : "否"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </>
      )}
    </div>
  );
}



