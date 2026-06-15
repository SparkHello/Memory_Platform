import { useCallback, useEffect, useMemo, useState } from "react";
import { Edit3, Plus, RefreshCcw, Save, Search, ShieldAlert, Trash2 } from "lucide-react";
import { MemoryApi } from "../../api";
import type { ProviderConfigResponse, ProviderModelSummary, RouteConfigPayload, RouteSummary } from "../../types";
import { PageHeader } from "../../components/PageHeader";
import { StatCard } from "../../components/StatCard";
import { EmptyBlock, ErrorBlock, LoadingBlock } from "../../components/StateBlocks";
import type { ConfirmFn } from "../../hooks/useConfirm";
import type { LoadState } from "../../hooks/useAsyncData";
import { errorMessage, moneyText } from "../../utils/format";
import { apiFormatText, EMPTY_ROUTE_DRAFT, routeToDraft } from "../../utils/gateway";
import type { RouteDraft } from "../../utils/gateway";
import type { Notify } from "../pageTypes";

type RouteGroup = {
  virtualModel: string;
  routes: RouteSummary[];
};

const PRIORITY_LABELS: Record<number, string> = {
  1: "首选",
  50: "默认",
  100: "备用"
};

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
  const [modelSearch, setModelSearch] = useState("");
  const [routeSearch, setRouteSearch] = useState("");
  const [providerFilter, setProviderFilter] = useState("all");

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
  const providers = data?.providers || [];
  const providerModels = data?.provider_models || [];
  const routes = data?.routes || [];
  const enabledProviderIds = useMemo(
    () => new Set(providers.filter((provider) => provider.enabled).map((provider) => provider.provider || provider.id)),
    [providers]
  );
  const enabledProviderModels = providerModels.filter(
    (model) => model.enabled && enabledProviderIds.has(model.provider)
  );
  const selectedModel = providerModels.find((model) => model.id === routeDraft.provider_model_id) || null;

  const providerNameById = useMemo(() => {
    const names = new Map<string, string>();
    providers.forEach((provider) => {
      names.set(provider.provider || provider.id, provider.name || provider.provider || provider.id);
    });
    return names;
  }, [providers]);

  const providerFilterOptions = useMemo(() => {
    const ids = new Set(enabledProviderModels.map((model) => model.provider));
    return Array.from(ids).sort();
  }, [enabledProviderModels]);

  const pickerModels = useMemo(() => {
    const term = modelSearch.trim().toLowerCase();
    return enabledProviderModels.filter((model) => {
      if (providerFilter !== "all" && model.provider !== providerFilter) {
        return false;
      }
      if (!term) {
        return true;
      }
      return [
        providerNameById.get(model.provider) || model.provider,
        model.provider,
        model.display_name,
        model.upstream_model,
        model.api_format
      ]
        .filter(Boolean)
        .some((value) => value.toLowerCase().includes(term));
    });
  }, [enabledProviderModels, modelSearch, providerFilter, providerNameById]);

  const visibleRoutes = useMemo(() => {
    const term = routeSearch.trim().toLowerCase();
    if (!term) return routes;
    return routes.filter((route) =>
      [route.virtual_model, route.provider, route.upstream_model]
        .filter(Boolean)
        .some((value) => value.toLowerCase().includes(term))
    );
  }, [routeSearch, routes]);

  const routeGroups = useMemo<RouteGroup[]>(() => {
    const groups = new Map<string, RouteSummary[]>();
    visibleRoutes.forEach((route) => {
      const items = groups.get(route.virtual_model) || [];
      items.push(route);
      groups.set(route.virtual_model, items);
    });
    return Array.from(groups.entries()).map(([virtualModel, groupRoutes]) => ({
      virtualModel,
      routes: groupRoutes.sort((left, right) => right.priority - left.priority)
    }));
  }, [visibleRoutes]);

  const saveRoute = async () => {
    if (!routeDraft.virtual_model.trim() || !routeDraft.provider_model_id.trim()) {
      notify("请填写对外模型名，并选择一个服务商模型", "error");
      return;
    }
    const selectedProviderModel = providerModels.find((model) => model.id === routeDraft.provider_model_id);
    if (!selectedProviderModel) {
      notify("选择的服务商模型不存在", "error");
      return;
    }
    const payload: RouteConfigPayload = {
      virtual_model: routeDraft.virtual_model.trim(),
      provider_model_id: selectedProviderModel.id,
      priority: Math.round(routeDraft.priority),
      min_balance: 0,
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
      setModelSearch("");
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

  const editRoute = (route: RouteSummary) => {
    setRouteDraft(routeToDraft(route));
    setModelSearch("");
    if (route.provider) {
      setProviderFilter(route.provider);
    }
  };

  const selectModel = (model: ProviderModelSummary) => {
    setRouteDraft({
      ...routeDraft,
      provider_model_id: model.id,
      provider: model.provider,
      upstream_model: model.upstream_model,
    });
  };

  return (
    <div className="page-stack">
      <PageHeader
        title="路由"
        subtitle="把对外统一模型名映射到一个或多个服务商模型。"
        action={
          <div className="button-row">
            <button className="secondary-button" type="button" onClick={load}>
              <RefreshCcw size={16} />
              刷新
            </button>
            <button
              className="primary-button"
              type="button"
              onClick={() => {
                setRouteDraft(EMPTY_ROUTE_DRAFT);
                setProviderFilter("all");
                setModelSearch("");
              }}
            >
              <Plus size={16} />
              新增路由
            </button>
          </div>
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

          <div className="route-management-layout">
            <section className="panel form-panel route-editor-panel">
              <div className="panel-header">
                <div>
                  <h2>{routeDraft.mode === "edit" ? "编辑路由" : "新增路由"}</h2>
                  <p className="muted">先命名客户端看到的模型，再选择真实服务商模型。</p>
                </div>
                <button
                  className="secondary-button compact"
                  type="button"
                  onClick={() => {
                    setRouteDraft(EMPTY_ROUTE_DRAFT);
                    setProviderFilter("all");
                    setModelSearch("");
                  }}
                >
                  <Plus size={14} />
                  新增
                </button>
              </div>

              <div className="route-draft-grid">
                <label className="field-block">
                  <span>对外模型名</span>
                  <input
                    value={routeDraft.virtual_model}
                    onChange={(event) => setRouteDraft({ ...routeDraft, virtual_model: event.target.value })}
                    placeholder="glm-5.1"
                  />
                </label>
                <label className="field-block">
                  <span>优先级</span>
                  <select
                    value={routeDraft.priority}
                    onChange={(event) => setRouteDraft({ ...routeDraft, priority: Number(event.target.value) })}
                  >
                    <option value={1}>首选 (最高)</option>
                    <option value={50}>默认</option>
                    <option value={100}>备用 (最低)</option>
                  </select>
                </label>
                <label className="checkbox-row route-enabled-row">
                  <input
                    type="checkbox"
                    checked={routeDraft.enabled}
                    onChange={(event) => setRouteDraft({ ...routeDraft, enabled: event.target.checked })}
                  />
                  启用
                </label>
              </div>

              <div className="model-picker">
                <div className="model-picker-header">
                  <div>
                    <strong>选择服务商模型</strong>
                    <span className="muted">只显示启用服务商下面的启用模型。</span>
                  </div>
                  {selectedModel && (
                    <span className="selected-model-pill">
                      {providerNameById.get(selectedModel.provider) || selectedModel.provider} /{" "}
                      {selectedModel.display_name || selectedModel.upstream_model}
                    </span>
                  )}
                </div>
                <label className="search-box model-picker-search">
                  <Search size={16} />
                  <input
                    value={modelSearch}
                    onChange={(event) => setModelSearch(event.target.value)}
                    placeholder="搜索服务商、模型名或真实模型 ID"
                  />
                </label>
                <div className="segmented-control provider-filter-tabs">
                  <button
                    className={providerFilter === "all" ? "active" : ""}
                    type="button"
                    onClick={() => setProviderFilter("all")}
                  >
                    全部
                  </button>
                  {providerFilterOptions.map((provider) => (
                    <button
                      className={providerFilter === provider ? "active" : ""}
                      key={provider}
                      type="button"
                      onClick={() => setProviderFilter(provider)}
                    >
                      {providerNameById.get(provider) || provider}
                    </button>
                  ))}
                </div>
                {providerModels.length === 0 && (
                  <div className="notice warning">
                    <ShieldAlert size={18} />
                    先到“服务商与模型”页面新增至少一个服务商模型，再配置路由。
                  </div>
                )}
                {providerModels.length > 0 && pickerModels.length === 0 && <EmptyBlock label="没有匹配的可用模型" />}
                {pickerModels.length > 0 && (
                  <div className="model-option-list">
                    {pickerModels.map((model) => {
                      const isSelected = routeDraft.provider_model_id === model.id;
                      return (
                        <button
                          className={`model-option ${isSelected ? "selected" : ""}`}
                          key={model.id}
                          type="button"
                          onClick={() => selectModel(model)}
                        >
                          <span className="model-option-title">
                            <strong>{model.display_name || model.upstream_model}</strong>
                            <span>{providerNameById.get(model.provider) || model.provider}</span>
                          </span>
                          <code>{model.upstream_model}</code>
                          <span className="model-option-meta">
                            {apiFormatText(model.api_format)} ·{" "}
                            {moneyText(model.input_price_per_million, model.currency)} /{" "}
                            {moneyText(model.output_price_per_million, model.currency)}
                          </span>
                        </button>
                      );
                    })}
                  </div>
                )}
              </div>

              <div className="button-row end">
                <button className="primary-button" type="button" disabled={busy} onClick={saveRoute}>
                  <Save size={16} />
                  保存路由
                </button>
              </div>
            </section>

            <section className="panel route-list-panel">
              <div className="panel-header">
                <div>
                  <h2>路由列表</h2>
                  <p className="muted">按对外模型名分组，优先级高的排在前面。</p>
                </div>
                <label className="search-box route-search-box">
                  <Search size={16} />
                  <input
                    value={routeSearch}
                    onChange={(event) => setRouteSearch(event.target.value)}
                    placeholder="搜索对外模型、服务商或真实模型"
                  />
                </label>
              </div>

              {routes.length === 0 && <EmptyBlock label="暂无路由" />}
              {routes.length > 0 && visibleRoutes.length === 0 && <EmptyBlock label="没有匹配的路由" />}
              {routeGroups.length > 0 && (
                <div className="route-group-list">
                  {routeGroups.map((group) => (
                    <section className="route-group" key={group.virtualModel}>
                      <div className="route-group-header">
                        <strong>{group.virtualModel}</strong>
                        <span>{group.routes.length} 条路由</span>
                      </div>
                      <div className="route-card-list">
                        {group.routes.map((route) => {
                          const providerModel = providerModels.find((model) => model.id === route.provider_model_id);
                          return (
                            <article
                              className={`route-card ${route.enabled === false ? "disabled" : ""}`}
                              key={route.id || `${route.virtual_model}-${route.provider}-${route.upstream_model}`}
                            >
                              <div className="route-card-main">
                                <div>
                                  <strong>{providerNameById.get(route.provider) || route.provider}</strong>
                                  <code>{route.upstream_model}</code>
                                </div>
                                <span className={`status-pill ${route.enabled === false ? "muted" : "ok"}`}>
                                  {route.enabled === false ? "禁用" : "启用"}
                                </span>
                              </div>
                              <div className="route-card-meta">
                                <span>{PRIORITY_LABELS[route.priority] ?? `优先级 ${route.priority}`}</span>
                                {providerModel && (
                                  <span>
                                    价格 {moneyText(providerModel.input_price_per_million, providerModel.currency)} /{" "}
                                    {moneyText(providerModel.output_price_per_million, providerModel.currency)}
                                  </span>
                                )}
                                <span>{providerModel ? apiFormatText(providerModel.api_format) : "旧路由"}</span>
                              </div>
                              <div className="button-row">
                                <button className="secondary-button compact" type="button" onClick={() => editRoute(route)}>
                                  <Edit3 size={14} />
                                  编辑
                                </button>
                                {route.id && !route.id.startsWith("toml:") && (
                                  <button
                                    className="danger-button compact"
                                    type="button"
                                    disabled={busy}
                                    onClick={() => deleteRoute(route.id || "")}
                                  >
                                    <Trash2 size={14} />
                                    删除
                                  </button>
                                )}
                              </div>
                            </article>
                          );
                        })}
                      </div>
                    </section>
                  ))}
                </div>
              )}
            </section>
          </div>
        </>
      )}
    </div>
  );
}
