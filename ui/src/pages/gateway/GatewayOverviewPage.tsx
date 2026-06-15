import { useCallback } from "react";
import { Download, ListChecks, RefreshCcw, Server, ShieldAlert, Upload } from "lucide-react";
import { MemoryApi } from "../../api";
import type { PageKey, ProviderConfigResponse } from "../../types";
import { PageHeader } from "../../components/PageHeader";
import { StatCard } from "../../components/StatCard";
import { ErrorBlock, LoadingBlock } from "../../components/StateBlocks";
import { useAsyncData } from "../../hooks/useAsyncData";
import { sourceText } from "../../utils/gateway";

export function GatewayOverviewPage({
  api,
  setPage
}: {
  api: MemoryApi;
  setPage: (page: PageKey) => void;
}) {
  const loadConfig = useCallback(() => api.providerConfig(), [api]);
  const state = useAsyncData<ProviderConfigResponse>(loadConfig);

  const data = state.data;
  const providers = data?.providers || [];
  const providerModels = data?.provider_models || [];
  const routes = data?.routes || [];

  return (
    <div className="page-stack">
      <PageHeader
        title="网关概览"
        subtitle="查看当前 provider 配置来源、服务商、模型和路由数量。"
        action={
          <button className="secondary-button" type="button" onClick={state.load}>
            <RefreshCcw size={16} />
            刷新
          </button>
        }
      />
      <div className="notice warning">
        <ShieldAlert size={18} />
        API key 保存在本机 SQLite，不会在页面或导出中回显；请避免把服务直接暴露到公网。
      </div>
      {state.loading && <LoadingBlock label="正在加载网关概览" />}
      {state.error && <ErrorBlock message={state.error} onRetry={state.load} />}
      {data && (
        <>
          <div className="stats-grid">
            <StatCard label="当前来源" value={sourceText(data.source)} />
            <StatCard label="服务商" value={providers.length} />
            <StatCard label="服务商模型" value={providerModels.length} />
            <StatCard label="启用路由" value={routes.filter((route) => route.enabled !== false).length} />
            <StatCard
              label="已配置密钥"
              value={providers.filter((provider) => provider.api_key_configured).length}
            />
          </div>

          <section className="panel">
            <div className="panel-header">
              <h2>入口</h2>
            </div>
            <div className="quick-actions">
              <button className="primary-button" type="button" onClick={() => setPage("providers")}>
                <Server size={16} />
                管理服务商与模型
              </button>
              <button className="secondary-button" type="button" onClick={() => setPage("routes")}>
                <ListChecks size={16} />
                管理路由
              </button>
              <button
                className="secondary-button"
                type="button"
                onClick={() => setPage("gateway-import-export")}
              >
                <Upload size={16} />
                导入 TOML
              </button>
              <button
                className="secondary-button"
                type="button"
                onClick={() => setPage("gateway-import-export")}
              >
                <Download size={16} />
                导出 TOML
              </button>
            </div>
          </section>
        </>
      )}
    </div>
  );
}
