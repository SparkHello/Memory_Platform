import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Edit3,
  FlaskConical,
  KeyRound,
  Plus,
  RefreshCcw,
  Save,
  Search,
  Trash2,
  XCircle
} from "lucide-react";
import { MemoryApi } from "../../api";
import type {
  ProviderConfigPayload,
  ProviderConfigResponse,
  ProviderModelConfigPayload,
  ProviderSummary
} from "../../types";
import { DecimalInput } from "../../components/FormControls";
import { PageHeader } from "../../components/PageHeader";
import { StatCard } from "../../components/StatCard";
import { EmptyBlock, ErrorBlock, LoadingBlock } from "../../components/StateBlocks";
import type { ConfirmFn } from "../../hooks/useConfirm";
import type { LoadState } from "../../hooks/useAsyncData";
import { clampNumber, errorMessage, moneyText } from "../../utils/format";
import {
  apiFormatText,
  createEmptyPriceTierDrafts,
  decimalInputValue,
  EMPTY_PROVIDER_DRAFT,
  EMPTY_PROVIDER_MODEL_DRAFT,
  ensureTwoPriceTierDrafts,
  priceTierDraftsToJson,
  pricingModeText,
  providerModelToDraft,
  providerToDraft
} from "../../utils/gateway";
import type { PriceTierDraft, ProviderDraft, ProviderModelDraft } from "../../utils/gateway";
import type { Notify } from "../pageTypes";

export function ProvidersPage({
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
  const [providerDraft, setProviderDraft] = useState<ProviderDraft>(EMPTY_PROVIDER_DRAFT);
  const [modelDraft, setModelDraft] = useState<ProviderModelDraft>({
    ...EMPTY_PROVIDER_MODEL_DRAFT,
    pricing_tiers: createEmptyPriceTierDrafts()
  });
  const [selectedProviderId, setSelectedProviderId] = useState<string | null>(null);
  const [providerFormOpen, setProviderFormOpen] = useState(false);
  const [providerSearch, setProviderSearch] = useState("");
  const [modelSearch, setModelSearch] = useState("");
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
  const providers = data?.providers || [];
  const providerModels = data?.provider_models || [];
  const routes = data?.routes || [];

  useEffect(() => {
    if (!data || providerFormOpen || selectedProviderId || providers.length === 0) {
      return;
    }
    const firstProvider = providers[0];
    setSelectedProviderId(firstProvider.provider || firstProvider.id);
  }, [data, providerFormOpen, providers, selectedProviderId]);

  const modelCounts = useMemo(() => {
    const counts = new Map<string, number>();
    providerModels.forEach((model) => {
      counts.set(model.provider, (counts.get(model.provider) || 0) + 1);
    });
    return counts;
  }, [providerModels]);

  const routeCounts = useMemo(() => {
    const counts = new Map<string, number>();
    routes.forEach((route) => {
      counts.set(route.provider, (counts.get(route.provider) || 0) + 1);
    });
    return counts;
  }, [routes]);

  const filteredProviders = useMemo(() => {
    const term = providerSearch.trim().toLowerCase();
    if (!term) return providers;
    return providers.filter((provider) => {
      const providerId = provider.provider || provider.id;
      return [providerId, provider.name, provider.base_url]
        .filter(Boolean)
        .some((value) => value.toLowerCase().includes(term));
    });
  }, [providerSearch, providers]);

  const selectedProvider = selectedProviderId
    ? providers.find((provider) => (provider.provider || provider.id) === selectedProviderId)
    : null;
  const selectedProviderModels = selectedProviderId
    ? providerModels.filter((model) => model.provider === selectedProviderId)
    : [];
  const visibleProviderModels = useMemo(() => {
    const term = modelSearch.trim().toLowerCase();
    if (!term) return selectedProviderModels;
    return selectedProviderModels.filter((model) =>
      [model.display_name, model.upstream_model, model.api_format, model.pricing_mode]
        .filter(Boolean)
        .some((value) => value.toLowerCase().includes(term))
    );
  }, [modelSearch, selectedProviderModels]);

  const startNewProvider = () => {
    setProviderDraft(EMPTY_PROVIDER_DRAFT);
    setProviderFormOpen(true);
    setSelectedProviderId(null);
    setModelSearch("");
  };

  const startEditProvider = (provider: ProviderSummary) => {
    setProviderDraft(providerToDraft(provider));
    setProviderFormOpen(true);
  };

  const cancelProviderForm = () => {
    setProviderDraft(EMPTY_PROVIDER_DRAFT);
    setProviderFormOpen(false);
    if (!selectedProviderId && providers.length > 0) {
      setSelectedProviderId(providers[0].provider || providers[0].id);
    }
  };

  const openProviderDetail = (provider: string) => {
    setSelectedProviderId(provider);
    setProviderFormOpen(false);
    setModelSearch("");
    setModelDraft({
      ...EMPTY_PROVIDER_MODEL_DRAFT,
      provider,
      pricing_tiers: createEmptyPriceTierDrafts()
    });
  };

  const saveProvider = async () => {
    const provider = providerDraft.provider.trim();
    if (!provider || !providerDraft.name.trim() || !providerDraft.base_url.trim()) {
      notify("请填写服务商 ID、名称和基础地址", "error");
      return;
    }
    setBusy(true);
    try {
      const payload: ProviderConfigPayload = {
        provider,
        name: providerDraft.name.trim(),
        base_url: providerDraft.base_url.trim(),
        enabled: providerDraft.enabled,
        timeout_seconds: clampNumber(providerDraft.timeout_seconds, 1, 600)
      };
      if (providerDraft.api_key.trim()) {
        payload.api_key = providerDraft.api_key.trim();
      }
      if (providerDraft.mode === "edit") {
        await api.updateProviderConfig(provider, payload);
      } else {
        await api.createProviderConfig(payload);
      }
      notify("服务商已保存", "success");
      setProviderDraft(EMPTY_PROVIDER_DRAFT);
      setSelectedProviderId(provider);
      setProviderFormOpen(false);
      await load();
    } catch (error) {
      notify(errorMessage(error), "error");
    } finally {
      setBusy(false);
    }
  };

  const clearProviderKey = async (provider: string) => {
    if (
      !(await confirm({
        title: "清除 API key",
        message: `确认清除 ${provider} 的 API key？`,
        tone: "warning",
        confirmLabel: "清除"
      }))
    ) {
      return;
    }
    setBusy(true);
    try {
      await api.updateProviderConfig(provider, { api_key: "" });
      notify("API key 已清除", "success");
      await load();
    } catch (error) {
      notify(errorMessage(error), "error");
    } finally {
      setBusy(false);
    }
  };

  const disableProvider = async (provider: string) => {
    if (
      !(await confirm({
        title: "禁用服务商",
        message: `确认禁用服务商 ${provider}？`,
        tone: "warning",
        confirmLabel: "禁用"
      }))
    ) {
      return;
    }
    setBusy(true);
    try {
      await api.disableProviderConfig(provider);
      notify("服务商已禁用", "success");
      await load();
    } catch (error) {
      notify(errorMessage(error), "error");
    } finally {
      setBusy(false);
    }
  };

  const deleteProvider = async (provider: string) => {
    const modelCount = modelCounts.get(provider) || 0;
    const routeCount = routeCounts.get(provider) || 0;
    if (
      !(await confirm({
        title: "删除服务商",
        message: `确认删除服务商 ${provider}？这会同时删除 ${modelCount} 个服务商模型和 ${routeCount} 条路由。用量记录和余额账本会保留。`,
        tone: "danger",
        confirmLabel: "删除"
      }))
    ) {
      return;
    }
    setBusy(true);
    try {
      await api.deleteProviderConfig(provider);
      notify("服务商已删除", "success");
      if (selectedProviderId === provider) {
        setSelectedProviderId(null);
      }
      setProviderDraft(EMPTY_PROVIDER_DRAFT);
      setProviderFormOpen(false);
      await load();
    } catch (error) {
      notify(errorMessage(error), "error");
    } finally {
      setBusy(false);
    }
  };

  const testProvider = async (provider: string, upstreamModel?: string) => {
    setBusy(true);
    try {
      const result = await api.testProviderConfig(provider, upstreamModel);
      notify(result.message, result.success ? "success" : "error");
    } catch (error) {
      notify(errorMessage(error), "error");
    } finally {
      setBusy(false);
    }
  };

  const saveProviderModel = async () => {
    const providerForModel = modelDraft.provider.trim() || selectedProviderId || "";
    if (!providerForModel || !modelDraft.upstream_model.trim()) {
      notify("请填写服务商和服务商模型 ID", "error");
      return;
    }
    const payload: ProviderModelConfigPayload = {
      provider: providerForModel,
      upstream_model: modelDraft.upstream_model.trim(),
      display_name: modelDraft.display_name.trim(),
      api_format: modelDraft.api_format,
      pricing_mode: modelDraft.pricing_mode,
      pricing_tiers_json:
        modelDraft.pricing_mode === "tiered" ? priceTierDraftsToJson(modelDraft.pricing_tiers) : "",
      input_price_per_million: clampNumber(
        decimalInputValue(modelDraft.input_price_per_million),
        0,
        1_000_000
      ),
      output_price_per_million: clampNumber(
        decimalInputValue(modelDraft.output_price_per_million),
        0,
        1_000_000
      ),
      cache_hit_price_per_million: clampNumber(
        decimalInputValue(modelDraft.cache_hit_price_per_million),
        0,
        1_000_000
      ),
      currency: modelDraft.currency.trim() || "CNY",
      enabled: modelDraft.enabled
    };
    setBusy(true);
    try {
      if (modelDraft.mode === "edit") {
        await api.updateProviderModelConfig(modelDraft.id, payload);
      } else {
        await api.createProviderModelConfig(payload);
      }
      notify("服务商模型已保存", "success");
      setModelDraft({
        ...EMPTY_PROVIDER_MODEL_DRAFT,
        provider: selectedProviderId || providerForModel,
        pricing_tiers: createEmptyPriceTierDrafts()
      });
      await load();
    } catch (error) {
      notify(errorMessage(error), "error");
    } finally {
      setBusy(false);
    }
  };

  const disableProviderModel = async (modelId: string) => {
    if (
      !(await confirm({
        title: "禁用服务商模型",
        message: "确认禁用这个服务商模型？已绑定的路由也会停止使用它。",
        tone: "warning",
        confirmLabel: "禁用"
      }))
    ) {
      return;
    }
    setBusy(true);
    try {
      await api.disableProviderModelConfig(modelId);
      notify("服务商模型已禁用", "success");
      await load();
    } catch (error) {
      notify(errorMessage(error), "error");
    } finally {
      setBusy(false);
    }
  };

  const deleteProviderModel = async (modelId: string, label: string) => {
    const routeCount = routes.filter((route) => route.provider_model_id === modelId).length;
    if (
      !(await confirm({
        title: "删除服务商模型",
        message: `确认删除 ${label}？这会同时删除 ${routeCount} 条绑定到它的路由。`,
        tone: "danger",
        confirmLabel: "删除"
      }))
    ) {
      return;
    }
    setBusy(true);
    try {
      await api.deleteProviderModelConfig(modelId);
      notify("服务商模型已删除", "success");
      if (modelDraft.id === modelId) {
        setModelDraft({
          ...EMPTY_PROVIDER_MODEL_DRAFT,
          provider: selectedProviderId || "",
          pricing_tiers: createEmptyPriceTierDrafts()
        });
      }
      await load();
    } catch (error) {
      notify(errorMessage(error), "error");
    } finally {
      setBusy(false);
    }
  };

  const startNewProviderModel = () => {
    setModelDraft({
      ...EMPTY_PROVIDER_MODEL_DRAFT,
      provider: selectedProviderId || "",
      pricing_tiers: createEmptyPriceTierDrafts()
    });
  };

  const updatePricingTier = (index: number, patch: Partial<PriceTierDraft>) => {
    setModelDraft((current) => ({
      ...current,
      pricing_tiers: ensureTwoPriceTierDrafts(current.pricing_tiers).map((tier, tierIndex) =>
        tierIndex === index ? { ...tier, ...patch } : tier
      )
    }));
  };

  const selectedProviderIdText = selectedProvider?.provider || selectedProvider?.id || "";

  return (
    <div className="page-stack">
      <PageHeader
        title="服务商与模型"
        subtitle="先管理上游服务商，再维护每个服务商下面的真实模型和价格。"
        action={
          <div className="button-row">
            <button className="secondary-button" type="button" onClick={load}>
              <RefreshCcw size={16} />
              刷新
            </button>
            <button className="primary-button" type="button" onClick={startNewProvider}>
              <Plus size={16} />
              新增服务商
            </button>
          </div>
        }
      />
      {state.loading && <LoadingBlock label="正在加载服务商" />}
      {state.error && <ErrorBlock message={state.error} onRetry={load} />}
      {data && (
        <>
          <div className="stats-grid">
            <StatCard label="服务商" value={providers.length} />
            <StatCard label="服务商模型" value={providerModels.length} />
            <StatCard label="已配置密钥" value={providers.filter((provider) => provider.api_key_configured).length} />
          </div>

          <div className="provider-management-layout">
            <section className="panel provider-list-panel">
              <div className="panel-header compact-header">
                <h2>服务商</h2>
                <button className="secondary-button compact" type="button" onClick={startNewProvider}>
                  <Plus size={14} />
                  新增
                </button>
              </div>
              <label className="search-box provider-search-box">
                <Search size={16} />
                <input
                  value={providerSearch}
                  onChange={(event) => setProviderSearch(event.target.value)}
                  placeholder="搜索 ID、名称或地址"
                />
              </label>
              {filteredProviders.length === 0 && <EmptyBlock label="没有匹配的服务商" />}
              {filteredProviders.length > 0 && (
                <div className="provider-list">
                  {filteredProviders.map((provider) => {
                    const providerId = provider.provider || provider.id;
                    const isActive = selectedProviderId === providerId && !providerFormOpen;
                    return (
                      <button
                        className={`provider-list-item ${isActive ? "active" : ""}`}
                        key={providerId}
                        type="button"
                        onClick={() => openProviderDetail(providerId)}
                      >
                        <span className="provider-list-title">
                          <strong>{provider.name || providerId}</strong>
                          <span className={`status-pill ${provider.enabled ? "ok" : "muted"}`}>
                            {provider.enabled ? "启用" : "禁用"}
                          </span>
                        </span>
                        <span className="provider-list-id">{providerId}</span>
                        <span className="provider-list-meta">
                          {modelCounts.get(providerId) || 0} 个模型 · {routeCounts.get(providerId) || 0} 条路由 ·{" "}
                          {provider.api_key_configured ? "已配密钥" : "未配密钥"}
                        </span>
                      </button>
                    );
                  })}
                </div>
              )}
            </section>

            <div className="provider-workspace">
              {providerFormOpen && (
                <section className="panel form-panel provider-form-panel">
                  <div className="panel-header">
                    <h2>{providerDraft.mode === "edit" ? "编辑服务商" : "新增服务商"}</h2>
                    <button className="ghost-button compact" type="button" onClick={cancelProviderForm}>
                      <XCircle size={14} />
                      取消
                    </button>
                  </div>
                  <div className="toolbar provider-form-toolbar">
                    <fieldset className="field-group">
                      <legend>基本信息</legend>
                      <label className="field-block small">
                        <span>服务商 ID</span>
                        <input
                          value={providerDraft.provider}
                          disabled={providerDraft.mode === "edit"}
                          onChange={(event) => setProviderDraft({ ...providerDraft, provider: event.target.value })}
                          placeholder="zhipu"
                        />
                      </label>
                      <label className="field-block small">
                        <span>名称</span>
                        <input
                          value={providerDraft.name}
                          onChange={(event) => setProviderDraft({ ...providerDraft, name: event.target.value })}
                          placeholder="智谱"
                        />
                      </label>
                      <label className="field-block small wide-field">
                        <span>基础地址</span>
                        <input
                          value={providerDraft.base_url}
                          onChange={(event) => setProviderDraft({ ...providerDraft, base_url: event.target.value })}
                          placeholder="https://open.bigmodel.cn/api/paas/v4"
                        />
                      </label>
                    </fieldset>
                    <fieldset className="field-group">
                      <legend>密钥与连接</legend>
                      <label className="field-block small">
                        <span>API Key</span>
                        <input
                          type="password"
                          value={providerDraft.api_key}
                          onChange={(event) => setProviderDraft({ ...providerDraft, api_key: event.target.value })}
                          placeholder={providerDraft.mode === "edit" ? "留空则不变" : "可稍后填写"}
                        />
                      </label>
                      <label className="field-block small">
                        <span>超时</span>
                        <input
                          type="number"
                          min={1}
                          value={providerDraft.timeout_seconds}
                          onChange={(event) =>
                            setProviderDraft({ ...providerDraft, timeout_seconds: Number(event.target.value) })
                          }
                        />
                      </label>
                      <label className="checkbox-row">
                        <input
                          type="checkbox"
                          checked={providerDraft.enabled}
                          onChange={(event) => setProviderDraft({ ...providerDraft, enabled: event.target.checked })}
                        />
                        启用
                      </label>
                    </fieldset>
                    <button className="primary-button" type="button" disabled={busy} onClick={saveProvider}>
                      <Save size={16} />
                      保存服务商
                    </button>
                  </div>
                </section>
              )}

              {!providerFormOpen && !selectedProvider && (
                <section className="panel">
                  <EmptyBlock label="选择一个服务商，或新增服务商开始配置" />
                </section>
              )}

              {!providerFormOpen && selectedProvider && (
                <>
                  <section className="panel provider-detail-panel">
                    <div className="panel-header">
                      <div>
                        <h2>{selectedProvider.name || selectedProviderIdText}</h2>
                        <p className="muted">{selectedProviderIdText}</p>
                      </div>
                      <div className="button-row">
                        <button
                          className="secondary-button compact"
                          type="button"
                          onClick={() => startEditProvider(selectedProvider)}
                        >
                          <Edit3 size={14} />
                          编辑
                        </button>
                        <button
                          className="secondary-button compact"
                          type="button"
                          disabled={busy}
                          onClick={() => testProvider(selectedProviderIdText)}
                        >
                          <FlaskConical size={14} />
                          测试
                        </button>
                        <button
                          className="secondary-button compact"
                          type="button"
                          disabled={busy || !selectedProvider.api_key_configured}
                          onClick={() => clearProviderKey(selectedProviderIdText)}
                        >
                          <KeyRound size={14} />
                          清除密钥
                        </button>
                        <button
                          className="warning-button compact"
                          type="button"
                          disabled={busy}
                          onClick={() => disableProvider(selectedProviderIdText)}
                        >
                          禁用
                        </button>
                        <button
                          className="danger-button compact"
                          type="button"
                          disabled={busy}
                          onClick={() => deleteProvider(selectedProviderIdText)}
                        >
                          <Trash2 size={14} />
                          删除
                        </button>
                      </div>
                    </div>
                    <dl className="field-list provider-detail-list">
                      <div>
                        <dt>基础地址</dt>
                        <dd>{selectedProvider.base_url}</dd>
                      </div>
                      <div>
                        <dt>API Key</dt>
                        <dd>{selectedProvider.api_key_configured ? "已配置" : "未配置"}</dd>
                      </div>
                      <div>
                        <dt>状态</dt>
                        <dd>{selectedProvider.enabled ? "启用" : "禁用"}</dd>
                      </div>
                      <div>
                        <dt>超时</dt>
                        <dd>{selectedProvider.timeout_seconds}s</dd>
                      </div>
                    </dl>
                  </section>

                  <section className="panel form-panel">
                    <div className="panel-header">
                      <div>
                        <h2>模型配置</h2>
                        <p className="muted">维护这个服务商下面可用于路由的真实模型。</p>
                      </div>
                      <button className="secondary-button" type="button" onClick={startNewProviderModel}>
                        <Plus size={16} />
                        新增模型
                      </button>
                    </div>
                    <div className="toolbar">
                      <fieldset className="field-group">
                        <legend>模型标识</legend>
                        <label className="field-block small">
                          <span>显示名称</span>
                          <input
                            value={modelDraft.display_name}
                            onChange={(event) =>
                              setModelDraft({ ...modelDraft, display_name: event.target.value })
                            }
                            placeholder="GLM 5.1"
                          />
                        </label>
                        <label className="field-block small">
                          <span>服务商模型 ID</span>
                          <input
                            value={modelDraft.upstream_model}
                            onChange={(event) =>
                              setModelDraft({ ...modelDraft, upstream_model: event.target.value })
                            }
                            placeholder="glm-5-1"
                          />
                        </label>
                      </fieldset>
                      <fieldset className="field-group">
                        <legend>接口与计费</legend>
                        <label className="field-block small">
                          <span>接口类型</span>
                          <select
                            value={modelDraft.api_format}
                            onChange={(event) =>
                              setModelDraft({
                                ...modelDraft,
                                api_format: event.target.value as ProviderModelDraft["api_format"]
                              })
                            }
                          >
                            <option value="openai_compatible">OpenAI-compatible</option>
                            <option value="claude_sdk">Claude SDK</option>
                          </select>
                        </label>
                        <label className="checkbox-row">
                          <input
                            type="checkbox"
                            checked={modelDraft.pricing_mode === "tiered"}
                            onChange={(event) =>
                              setModelDraft({
                                ...modelDraft,
                                pricing_mode: event.target.checked ? "tiered" : "flat",
                                pricing_tiers: ensureTwoPriceTierDrafts(modelDraft.pricing_tiers)
                              })
                            }
                          />
                          分级价格
                        </label>
                        <label className="checkbox-row">
                          <input
                            type="checkbox"
                            checked={modelDraft.enabled}
                            onChange={(event) => setModelDraft({ ...modelDraft, enabled: event.target.checked })}
                          />
                          启用
                        </label>
                      </fieldset>
                    </div>

                    <div className="toolbar">
                      <fieldset className="field-group">
                        <legend>定价</legend>
                        <label className="field-block small">
                          <span>输入价格 / 1M</span>
                          <DecimalInput
                            value={modelDraft.input_price_per_million}
                            onChange={(value) => setModelDraft({ ...modelDraft, input_price_per_million: value })}
                          />
                        </label>
                        <label className="field-block small">
                          <span>输出价格 / 1M</span>
                          <DecimalInput
                            value={modelDraft.output_price_per_million}
                            onChange={(value) => setModelDraft({ ...modelDraft, output_price_per_million: value })}
                          />
                        </label>
                        <label className="field-block small">
                          <span>缓存命中价格 / 1M</span>
                          <DecimalInput
                            value={modelDraft.cache_hit_price_per_million}
                            onChange={(value) => setModelDraft({ ...modelDraft, cache_hit_price_per_million: value })}
                          />
                        </label>
                        <label className="field-block small">
                          <span>币种</span>
                          <input
                            value={modelDraft.currency}
                            onChange={(event) => setModelDraft({ ...modelDraft, currency: event.target.value })}
                          />
                        </label>
                      </fieldset>
                      <button className="primary-button" type="button" disabled={busy} onClick={saveProviderModel}>
                        <Save size={16} />
                        保存模型
                      </button>
                    </div>

                    {modelDraft.pricing_mode === "tiered" && (
                      <div className="tier-editor">
                        {ensureTwoPriceTierDrafts(modelDraft.pricing_tiers).map((tier, index) => (
                          <div className="tier-row" key={index}>
                            <strong>第 {index + 1} 档</strong>
                            <label className="field-block small">
                              <span>Token 上限</span>
                              <DecimalInput
                                step="1"
                                placeholder={index === 0 ? "1000000" : "不限"}
                                emptyValueOnBlur={index === 0 ? "0" : ""}
                                value={tier.up_to_tokens}
                                onChange={(value) => updatePricingTier(index, { up_to_tokens: value })}
                              />
                            </label>
                            <label className="field-block small">
                              <span>输入价格 / 1M</span>
                              <DecimalInput
                                value={tier.input_price_per_million}
                                onChange={(value) => updatePricingTier(index, { input_price_per_million: value })}
                              />
                            </label>
                            <label className="field-block small">
                              <span>输出价格 / 1M</span>
                              <DecimalInput
                                value={tier.output_price_per_million}
                                onChange={(value) => updatePricingTier(index, { output_price_per_million: value })}
                              />
                            </label>
                            <label className="field-block small">
                              <span>缓存命中价格 / 1M</span>
                              <DecimalInput
                                value={tier.cache_hit_price_per_million}
                                onChange={(value) => updatePricingTier(index, { cache_hit_price_per_million: value })}
                              />
                            </label>
                          </div>
                        ))}
                      </div>
                    )}

                    <div className="model-list-toolbar">
                      <div>
                        <strong>{selectedProviderModels.length} 个服务商模型</strong>
                        <span className="muted">
                          {selectedProviderModels.filter((model) => model.enabled).length} 个启用
                        </span>
                      </div>
                      <label className="search-box model-search-box">
                        <Search size={16} />
                        <input
                          value={modelSearch}
                          onChange={(event) => setModelSearch(event.target.value)}
                          placeholder="搜索模型 ID、名称或接口类型"
                        />
                      </label>
                    </div>

                    {selectedProviderModels.length === 0 && <EmptyBlock label="暂无服务商模型" />}
                    {selectedProviderModels.length > 0 && visibleProviderModels.length === 0 && (
                      <EmptyBlock label="没有匹配的服务商模型" />
                    )}
                    {visibleProviderModels.length > 0 && (
                      <div className="provider-model-list">
                        {visibleProviderModels.map((model) => (
                          <article className={`provider-model-card ${model.enabled ? "" : "disabled"}`} key={model.id}>
                            <div className="provider-model-main">
                              <div>
                                <strong>{model.display_name || model.upstream_model}</strong>
                                <code>{model.upstream_model}</code>
                              </div>
                              <span className={`status-pill ${model.enabled ? "ok" : "muted"}`}>
                                {model.enabled ? "启用" : "禁用"}
                              </span>
                            </div>
                            <div className="provider-model-meta">
                              <span>{apiFormatText(model.api_format)}</span>
                              <span>{pricingModeText(model.pricing_mode)}</span>
                              <span>
                                {moneyText(model.input_price_per_million, model.currency)} /{" "}
                                {moneyText(model.output_price_per_million, model.currency)} /{" "}
                                {moneyText(model.cache_hit_price_per_million, model.currency)}
                              </span>
                            </div>
                            <div className="button-row">
                              <button
                                className="secondary-button compact"
                                type="button"
                                onClick={() => setModelDraft(providerModelToDraft(model))}
                              >
                                <Edit3 size={14} />
                                编辑
                              </button>
                              <button
                                className="secondary-button compact"
                                type="button"
                                disabled={busy}
                                onClick={() => testProvider(selectedProviderIdText, model.upstream_model)}
                              >
                                <FlaskConical size={14} />
                                测试
                              </button>
                              <button
                                className="warning-button compact"
                                type="button"
                                disabled={busy}
                                onClick={() => disableProviderModel(model.id)}
                              >
                                禁用
                              </button>
                              <button
                                className="danger-button compact"
                                type="button"
                                disabled={busy}
                                onClick={() =>
                                  deleteProviderModel(model.id, model.display_name || model.upstream_model)
                                }
                              >
                                <Trash2 size={14} />
                                删除
                              </button>
                            </div>
                          </article>
                        ))}
                      </div>
                    )}
                  </section>
                </>
              )}
            </div>
          </div>
        </>
      )}
    </div>
  );
}
