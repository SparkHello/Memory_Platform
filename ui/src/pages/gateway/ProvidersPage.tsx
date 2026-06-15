import { useCallback, useEffect, useState } from "react";
import { RefreshCcw, Save, ShieldAlert } from "lucide-react";
import { MemoryApi } from "../../api";
import type {
  ProviderConfigPayload,
  ProviderConfigResponse,
  ProviderModelConfigPayload,
  RouteConfigPayload
} from "../../types";
import { DecimalInput } from "../../components/FormControls";
import { PageHeader } from "../../components/PageHeader";
import { StatCard } from "../../components/StatCard";
import { EmptyBlock, ErrorBlock, LoadingBlock } from "../../components/StateBlocks";
import type { ConfirmFn } from "../../hooks/useConfirm";
import type { LoadState } from "../../hooks/useAsyncData";
import {
  clampNumber,
  errorMessage,
  moneyText
} from "../../utils/format";
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
  const [modelDraft, setModelDraft] = useState<ProviderModelDraft>(EMPTY_PROVIDER_MODEL_DRAFT);
  const [selectedProviderId, setSelectedProviderId] = useState<string | null>(null);
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
  const selectedProvider = selectedProviderId
    ? providers.find((provider) => (provider.provider || provider.id) === selectedProviderId)
    : null;
  const selectedProviderModels = selectedProviderId
    ? providerModels.filter((model) => model.provider === selectedProviderId)
    : [];

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
      await api.deleteProviderConfig(provider);
      notify("服务商已禁用", "success");
      await load();
    } catch (error) {
      notify(errorMessage(error), "error");
    } finally {
      setBusy(false);
    }
  };

  const testProvider = async (provider: string) => {
    setBusy(true);
    try {
      const result = await api.testProviderConfig(provider);
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
        provider: selectedProviderId || "",
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
      await api.deleteProviderModelConfig(modelId);
      notify("服务商模型已禁用", "success");
      await load();
    } catch (error) {
      notify(errorMessage(error), "error");
    } finally {
      setBusy(false);
    }
  };

  const openProviderDetail = (provider: string) => {
    setSelectedProviderId(provider);
    setModelDraft({
      ...EMPTY_PROVIDER_MODEL_DRAFT,
      provider,
      pricing_tiers: createEmptyPriceTierDrafts()
    });
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

  if (data && selectedProvider) {
    const providerId = selectedProvider.provider || selectedProvider.id;
    return (
      <div className="page-stack">
        <div className="breadcrumb">
          <button type="button" onClick={() => setSelectedProviderId(null)}>
            服务商
          </button>
          <span className="separator">›</span>
          <span>{selectedProvider.name || (selectedProvider.provider || selectedProvider.id)}</span>
        </div>
        <PageHeader
          title={selectedProvider.name || providerId}
          subtitle="配置这个服务商下面的真实模型 ID、接口类型和价格。"
          action={
            <div className="button-row">
              <button className="secondary-button" type="button" onClick={load}>
                <RefreshCcw size={16} />
                刷新
              </button>
            </div>
          }
        />

        <div className="stats-grid">
          <StatCard label="服务商模型" value={selectedProviderModels.length} />
          <StatCard
            label="OpenAI-compatible"
            value={
              selectedProviderModels.filter((model) => model.api_format === "openai_compatible").length
            }
          />
          <StatCard
            label="Claude SDK"
            value={selectedProviderModels.filter((model) => model.api_format === "claude_sdk").length}
          />
        </div>

        <section className="panel">
          <div className="panel-header">
            <h2>服务商信息</h2>
            <div className="button-row">
              <button
                className="secondary-button compact"
                type="button"
                onClick={() => {
                  setProviderDraft(providerToDraft(selectedProvider));
                  setSelectedProviderId(null);
                }}
              >
                编辑
              </button>
              <button
                className="secondary-button compact"
                type="button"
                disabled={busy}
                onClick={() => testProvider(providerId)}
              >
                测试
              </button>
            </div>
          </div>
          <dl className="field-list compact">
            <div>
              <dt>服务商 ID</dt>
              <dd>{providerId}</dd>
            </div>
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
          </dl>
        </section>

        <section className="panel form-panel">
          <div className="panel-header">
            <h2>模型配置</h2>
            <button className="secondary-button" type="button" onClick={startNewProviderModel}>
              新增
            </button>
          </div>
          <div className="toolbar">
            <fieldset className="field-group">
              <legend>模型标识</legend>
              <label className="field-block small">
                <span>显示名称</span>
                <input
                  value={modelDraft.display_name}
                  onChange={(event) => setModelDraft({ ...modelDraft, display_name: event.target.value })}
                  placeholder="GLM 5.1"
                />
              </label>
              <label className="field-block small">
                <span>服务商模型 ID</span>
                <input
                  value={modelDraft.upstream_model}
                  onChange={(event) => setModelDraft({ ...modelDraft, upstream_model: event.target.value })}
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
            </fieldset>
            <fieldset className="field-group">
              <legend>状态</legend>
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
                      onChange={(value) =>
                        updatePricingTier(index, { input_price_per_million: value })
                      }
                    />
                  </label>
                  <label className="field-block small">
                    <span>输出价格 / 1M</span>
                    <DecimalInput
                      value={tier.output_price_per_million}
                      onChange={(value) =>
                        updatePricingTier(index, { output_price_per_million: value })
                      }
                    />
                  </label>
                </div>
              ))}
            </div>
          )}

          {selectedProviderModels.length === 0 && <EmptyBlock label="暂无服务商模型" />}
          {selectedProviderModels.length > 0 && (
            <div className="table-wrap">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>显示名称</th>
                    <th>服务商模型 ID</th>
                    <th>接口类型</th>
                    <th>计费</th>
                    <th>价格 / 1M</th>
                    <th>启用</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {selectedProviderModels.map((model) => (
                    <tr key={model.id}>
                      <td>{model.display_name || "-"}</td>
                      <td>{model.upstream_model}</td>
                      <td>{apiFormatText(model.api_format)}</td>
                      <td>{pricingModeText(model.pricing_mode)}</td>
                      <td>
                        {moneyText(model.input_price_per_million, model.currency)} /{" "}
                        {moneyText(model.output_price_per_million, model.currency)}
                      </td>
                      <td>{model.enabled ? "是" : "否"}</td>
                      <td>
                        <div className="button-row">
                          <button
                            className="secondary-button compact"
                            type="button"
                            onClick={() => setModelDraft(providerModelToDraft(model))}
                          >
                            编辑
                          </button>
                          <button
                            className="warning-button compact"
                            type="button"
                            disabled={busy}
                            onClick={() => disableProviderModel(model.id)}
                          >
                            禁用
                          </button>
                        </div>
                      </td>
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

  return (
    <div className="page-stack">
      <PageHeader
        title="服务商"
        subtitle="配置上游 API 服务商；点进某个服务商后再配置它下面的真实模型和价格。"
        action={
          <button className="secondary-button" type="button" onClick={load}>
            <RefreshCcw size={16} />
            刷新
          </button>
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

          <section className="panel form-panel">
            <div className="panel-header">
              <h2>服务商管理</h2>
              <button
                className="secondary-button"
                type="button"
                onClick={() => setProviderDraft(EMPTY_PROVIDER_DRAFT)}
              >
                新增
              </button>
            </div>
            <div className="toolbar">
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
              </fieldset>
              <fieldset className="field-group">
                <legend>状态</legend>
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
            {providers.length === 0 && <EmptyBlock label="暂无服务商配置" />}
            {providers.length > 0 && (
              <div className="table-wrap">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>ID</th>
                      <th>名称</th>
                      <th>启用</th>
                      <th>基础地址</th>
                      <th>API Key</th>
                      <th>超时</th>
                      <th>操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {providers.map((provider) => (
                      <tr key={provider.id || provider.provider}>
                        <td>{provider.provider || provider.id}</td>
                        <td>{provider.name}</td>
                        <td>{provider.enabled ? "是" : "否"}</td>
                        <td>{provider.base_url}</td>
                        <td>{provider.api_key_configured ? "已配置" : "未配置"}</td>
                        <td>{provider.timeout_seconds}s</td>
                        <td>
                          <div className="button-row">
                            <button
                              className="secondary-button compact"
                              type="button"
                              onClick={() => setProviderDraft(providerToDraft(provider))}
                            >
                              编辑
                            </button>
                            <button
                              className="secondary-button compact"
                              type="button"
                              onClick={() => openProviderDetail(provider.provider || provider.id)}
                            >
                              模型
                            </button>
                            <button
                              className="secondary-button compact"
                              type="button"
                              disabled={busy}
                              onClick={() => testProvider(provider.provider || provider.id)}
                            >
                              测试
                            </button>
                            <button
                              className="secondary-button compact"
                              type="button"
                              disabled={busy}
                              onClick={() => clearProviderKey(provider.provider || provider.id)}
                            >
                              清除密钥
                            </button>
                            <button
                              className="warning-button compact"
                              type="button"
                              disabled={busy}
                              onClick={() => disableProvider(provider.provider || provider.id)}
                            >
                              禁用
                            </button>
                          </div>
                        </td>
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



