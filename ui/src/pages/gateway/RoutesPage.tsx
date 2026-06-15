import { useCallback, useEffect, useState } from "react";
import { RefreshCcw, Save, ShieldAlert } from "lucide-react";
import { MemoryApi } from "../../api";
import type { ProviderConfigResponse, RouteConfigPayload } from "../../types";
import { PageHeader } from "../../components/PageHeader";
import { StatCard } from "../../components/StatCard";
import { EmptyBlock, ErrorBlock, LoadingBlock } from "../../components/StateBlocks";
import type { ConfirmFn } from "../../hooks/useConfirm";
import type { LoadState } from "../../hooks/useAsyncData";
import { clampNumber, errorMessage, moneyText } from "../../utils/format";
import {
  EMPTY_ROUTE_DRAFT,
  providerModelLabel,
  routeToDraft
} from "../../utils/gateway";
import type { RouteDraft } from "../../utils/gateway";
import type { Notify } from "../pageTypes";

export function RoutesPage({
  api,
  notify,
  confirm
}: {
  api: MemoryApi;
  notify: Notify;
  confirm: ConfirmFn;
}) {
  const [state, setState] = useState<LoadState<ProviderConfigResponse>>({
    loading: true,
    error: null,
    data: null
  });
  const [routeDraft, setRouteDraft] = useState<RouteDraft>(EMPTY_ROUTE_DRAFT);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setState({ loading: true, error: null, data: null });
    try {
      setState({ loading: false, error: null, data: await api.providerConfig() });
    } catch (error) {
      setState({ loading: false, error: errorMessage(error), data: null });
    }
  }, [api]);

  useEffect(() => {
    void load();
  }, [load]);

  const data = state.data;
  const providerModels = data?.provider_models || [];
  const routes = data?.routes || [];
  const enabledProviderModels = providerModels.filter((model) => model.enabled);

  const saveRoute = async () => {
    if (!routeDraft.virtual_model.trim() || !routeDraft.provider_model_id.trim()) {
      notify("请填写对外模型名，并选择一个服务商模型", "error");
      return;
    }
    const selectedModel = providerModels.find((model) => model.id === routeDraft.provider_model_id);
    if (!selectedModel) {
      notify("选择的服务商模型不存在", "error");
      return;
    }
    const payload: RouteConfigPayload = {
      virtual_model: routeDraft.virtual_model.trim(),
      provider_model_id: selectedModel.id,
      priority: Math.round(routeDraft.priority),
      min_balance: clampNumber(routeDraft.min_balance, 0, 1_000_000_000),
      enabled: routeDraft.enabled
    };
    setBusy(true);
    try {
      if (routeDraft.mode === "edit") {
        await api.updateRouteConfig(routeDraft.id, payload);
      } else {
        await api.createRouteConfig(payload);
      }
      notify("路由已保存", "success");
      setRouteDraft(EMPTY_ROUTE_DRAFT);
      await load();
    } catch (error) {
      notify(errorMessage(error), "error");
    } finally {
      setBusy(false);
    }
  };

  const deleteRoute = async (routeId: string) => {
    if (
      !(await confirm({
        title: "删除路由",
        message: "确认删除这条路由？",
        tone: "danger",
        confirmLabel: "删除"
      }))
    ) {
      return;
    }
    setBusy(true);
    try {
      await api.deleteRouteConfig(routeId);
      notify("路由已删除", "success");
      await load();
    } catch (error) {
      notify(errorMessage(error), "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="page-stack">
      <PageHeader
        title="路由"
        subtitle="把对外统一模型名映射到一个或多个服务商模型。"
        action={
          <button className="secondary-button" type="button" onClick={load}>
            <RefreshCcw size={16} />
            刷新
          </button>
        }
      />
      {state.loading && <LoadingBlock label="正在加载路由" />}
      {state.error && <ErrorBlock message={state.error} onRetry={load} />}
      {data && (
        <>
          <div className="stats-grid">
            <StatCard label="对外模型" value={new Set(routes.map((route) => route.virtual_model)).size} />
            <StatCard label="路由" value={routes.length} />
            <StatCard label="可选服务商模型" value={enabledProviderModels.length} />
          </div>

          <section className="panel form-panel">
            <div className="panel-header">
              <h2>路由管理</h2>
              <button
                className="secondary-button"
                type="button"
                onClick={() => setRouteDraft(EMPTY_ROUTE_DRAFT)}
              >
                新增
              </button>
            </div>
            <div className="toolbar">
              <fieldset className="field-group">
                <legend>路由定义</legend>
                <label className="field-block small">
                  <span>对外模型名</span>
                  <input
                    value={routeDraft.virtual_model}
                    onChange={(event) => setRouteDraft({ ...routeDraft, virtual_model: event.target.value })}
                    placeholder="glm-5.1"
                  />
                </label>
                <label className="field-block small wide-field">
                  <span>服务商模型</span>
                  <select
                    value={routeDraft.provider_model_id}
                    onChange={(event) => setRouteDraft({ ...routeDraft, provider_model_id: event.target.value })}
                  >
                    <option value="">选择服务商模型</option>
                    {enabledProviderModels.map((model) => (
                      <option key={model.id} value={model.id}>
                        {providerModelLabel(model)}
                      </option>
                    ))}
                  </select>
                </label>
              </fieldset>
              <fieldset className="field-group">
                <legend>策略</legend>
                <label className="field-block small">
                  <span>优先级</span>
                  <input
                    type="number"
                    value={routeDraft.priority}
                    onChange={(event) => setRouteDraft({ ...routeDraft, priority: Number(event.target.value) })}
                  />
                </label>
                <label className="field-block small">
                  <span>最低余额</span>
                  <input
                    type="number"
                    min={0}
                    step="0.000001"
                    value={routeDraft.min_balance}
                    onChange={(event) => setRouteDraft({ ...routeDraft, min_balance: Number(event.target.value) })}
                  />
                </label>
              </fieldset>
              <fieldset className="field-group">
                <legend>状态</legend>
                <label className="checkbox-row">
                  <input
                    type="checkbox"
                    checked={routeDraft.enabled}
                    onChange={(event) => setRouteDraft({ ...routeDraft, enabled: event.target.checked })}
                  />
                  启用
                </label>
              </fieldset>
              <button className="primary-button" type="button" disabled={busy} onClick={saveRoute}>
                <Save size={16} />
                保存路由
              </button>
            </div>
            {providerModels.length === 0 && (
              <div className="notice warning">
                <ShieldAlert size={18} />
                先到“服务商”页面新增至少一个服务商模型，再配置路由。
              </div>
            )}
            {routes.length === 0 && <EmptyBlock label="暂无路由" />}
            {routes.length > 0 && (
              <div className="table-wrap">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>对外模型名</th>
                      <th>服务商模型</th>
                      <th>服务商</th>
                      <th>真实模型 ID</th>
                      <th>优先级</th>
                      <th>价格 / 1M</th>
                      <th>最低余额</th>
                      <th>启用</th>
                      <th>操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {routes.map((route) => {
                      const providerModel = providerModels.find((model) => model.id === route.provider_model_id);
                      return (
                        <tr key={route.id || `${route.virtual_model}-${route.provider}-${route.upstream_model}`}>
                          <td>{route.virtual_model}</td>
                          <td>{providerModel ? providerModelLabel(providerModel) : "旧路由"}</td>
                          <td>{route.provider}</td>
                          <td>{route.upstream_model}</td>
                          <td>{route.priority}</td>
                          <td>
                            {moneyText(route.input_price_per_million, route.currency)} /{" "}
                            {moneyText(route.output_price_per_million, route.currency)}
                          </td>
                          <td>{moneyText(route.min_balance, route.currency)}</td>
                          <td>{route.enabled === false ? "否" : "是"}</td>
                          <td>
                            <div className="button-row">
                              <button
                                className="secondary-button compact"
                                type="button"
                                onClick={() => setRouteDraft(routeToDraft(route))}
                              >
                                编辑
                              </button>
                              {route.id && !route.id.startsWith("toml:") && (
                                <button
                                  className="danger-button compact"
                                  type="button"
                                  disabled={busy}
                                  onClick={() => deleteRoute(route.id || "")}
                                >
                                  删除
                                </button>
                              )}
                            </div>
                          </td>
                        </tr>
                      );
                    })}
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



