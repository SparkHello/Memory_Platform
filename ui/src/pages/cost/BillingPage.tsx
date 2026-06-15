import { useCallback, useEffect, useState } from "react";
import { RefreshCcw, Save } from "lucide-react";
import { MemoryApi } from "../../api";
import type { BalanceRecord, UsageEvent, UsageSummary } from "../../types";
import { badge } from "../../components/Badge";
import { PageHeader } from "../../components/PageHeader";
import { StatCard } from "../../components/StatCard";
import { EmptyBlock, ErrorBlock, LoadingBlock } from "../../components/StateBlocks";
import type { LoadState } from "../../hooks/useAsyncData";
import { dateText, errorMessage, moneyText, numberText } from "../../utils/format";
import type { Notify } from "../pageTypes";

export function BillingPage({
  api,
  notify
}: {
  api: MemoryApi;
  notify: Notify;
}) {
  const [state, setState] = useState<LoadState<BalanceRecord[]>>({
    loading: true,
    error: null,
    data: null
  });
  const [draft, setDraft] = useState({
    provider: "",
    amount: "",
    currency: "CNY",
    reason: "手动调整"
  });
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    setState({ loading: true, error: null, data: null });
    try {
      setState({ loading: false, error: null, data: await api.balances() });
    } catch (error) {
      setState({ loading: false, error: errorMessage(error), data: null });
    }
  }, [api]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    const firstProvider = state.data?.[0]?.provider;
    if (firstProvider && !draft.provider) {
      setDraft((current) => ({ ...current, provider: firstProvider }));
    }
  }, [draft.provider, state.data]);

  const adjust = async () => {
    const provider = draft.provider.trim();
    const amount = Number(draft.amount);
    if (!provider || Number.isNaN(amount)) {
      notify("请填写服务商和有效金额", "error");
      return;
    }
    setSaving(true);
    try {
      await api.adjustBalance(provider, {
        amount_delta: amount,
        currency: draft.currency.trim() || "CNY",
        reason: draft.reason.trim()
      });
      notify("余额已调整", "success");
      setDraft((current) => ({ ...current, amount: "", reason: "手动调整" }));
      await load();
    } catch (error) {
      notify(errorMessage(error), "error");
    } finally {
      setSaving(false);
    }
  };

  const balances = state.data || [];

  return (
    <div className="page-stack">
      <PageHeader
        title="余额账本"
        subtitle="本地服务商余额账本和手动调整。"
        action={
          <button className="secondary-button" type="button" onClick={load}>
            <RefreshCcw size={16} />
            刷新
          </button>
        }
      />
      <section className="panel form-panel">
        <div className="panel-header">
          <h2>手动调整余额</h2>
        </div>
        <div className="toolbar">
          <fieldset className="field-group">
            <legend>调整信息</legend>
            <label className="field-block small">
              <span>服务商</span>
              <input
                value={draft.provider}
                onChange={(event) => setDraft({ ...draft, provider: event.target.value })}
                placeholder="zhipu"
                list="provider-balance-list"
              />
              <datalist id="provider-balance-list">
                {balances.map((balance) => (
                  <option key={balance.provider} value={balance.provider} />
                ))}
              </datalist>
            </label>
            <label className="field-block small">
              <span>调整金额</span>
              <input
                type="number"
                step="0.000001"
                value={draft.amount}
                onChange={(event) => setDraft({ ...draft, amount: event.target.value })}
                placeholder="100"
              />
            </label>
            <label className="field-block small">
              <span>币种</span>
              <input
                value={draft.currency}
                onChange={(event) => setDraft({ ...draft, currency: event.target.value })}
              />
            </label>
            <label className="field-block small">
              <span>原因</span>
              <input
                value={draft.reason}
                onChange={(event) => setDraft({ ...draft, reason: event.target.value })}
              />
            </label>
          </fieldset>
          <button className="primary-button" type="button" disabled={saving} onClick={adjust}>
            <Save size={16} />
            保存
          </button>
        </div>
      </section>

      <section className="panel">
        <div className="panel-header">
          <h2>余额</h2>
        </div>
        {state.loading && <LoadingBlock label="正在加载余额" />}
        {state.error && <ErrorBlock message={state.error} onRetry={load} />}
        {!state.loading && !state.error && balances.length === 0 && <EmptyBlock label="暂无余额记录" />}
        {balances.length > 0 && (
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>服务商</th>
                  <th>余额</th>
                  <th>币种</th>
                  <th>更新时间</th>
                </tr>
              </thead>
              <tbody>
                {balances.map((balance) => (
                  <tr key={balance.provider}>
                    <td>{balance.provider}</td>
                    <td>{numberText(balance.balance)}</td>
                    <td>{balance.currency}</td>
                    <td>{dateText(balance.updated_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}



