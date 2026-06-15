import { useCallback, useState } from "react";
import { Clipboard, Download, RefreshCcw, ShieldAlert, Upload } from "lucide-react";
import { MemoryApi } from "../../api";
import type { ProviderConfigResponse } from "../../types";
import { PageHeader } from "../../components/PageHeader";
import { StatCard } from "../../components/StatCard";
import { ErrorBlock, LoadingBlock } from "../../components/StateBlocks";
import type { ConfirmFn } from "../../hooks/useConfirm";
import { useAsyncData } from "../../hooks/useAsyncData";
import { copyText } from "../../utils/files";
import { errorMessage } from "../../utils/format";
import { sourceText } from "../../utils/gateway";
import type { Notify } from "../pageTypes";

export function GatewayImportExportPage({
  api,
  notify,
  confirm
}: {
  api: MemoryApi;
  notify: Notify;
  confirm: ConfirmFn;
}) {
  const loadConfig = useCallback(() => api.providerConfig(), [api]);
  const state = useAsyncData<ProviderConfigResponse>(loadConfig);
  const [exportText, setExportText] = useState("");
  const [busy, setBusy] = useState(false);

  const importToml = async () => {
    if (
      !(await confirm({
        title: "从 TOML 导入",
        message: "从 providers.toml 导入会合并到 UI 配置，真实 API key 不会导入。继续？",
        tone: "warning",
        confirmLabel: "导入"
      }))
    ) {
      return;
    }
    setBusy(true);
    try {
      const result = await api.importProviderConfigToml();
      notify(`已导入 ${result.providers} 个服务商、${result.routes} 条路由`, "success");
      await state.load();
    } catch (error) {
      notify(errorMessage(error), "error");
    } finally {
      setBusy(false);
    }
  };

  const exportToml = async () => {
    setBusy(true);
    try {
      setExportText(await api.exportProviderConfigToml());
      notify("已生成 TOML", "success");
    } catch (error) {
      notify(errorMessage(error), "error");
    } finally {
      setBusy(false);
    }
  };

  const data = state.data;

  return (
    <div className="page-stack">
      <PageHeader
        title="导入 / 导出"
        subtitle="从 providers.toml 合并配置，或导出不含真实 API key 的 TOML。"
        action={
          <button className="secondary-button" type="button" onClick={state.load}>
            <RefreshCcw size={16} />
            刷新
          </button>
        }
      />
      <div className="notice warning">
        <ShieldAlert size={18} />
        导出结果不会包含真实 API key，只会保留环境变量占位信息。
      </div>
      {state.loading && <LoadingBlock label="正在加载网关配置" />}
      {state.error && <ErrorBlock message={state.error} onRetry={state.load} />}
      {data && (
        <>
          <div className="stats-grid">
            <StatCard label="当前来源" value={sourceText(data.source)} />
            <StatCard label="服务商" value={data.providers.length} />
            <StatCard label="服务商模型" value={data.provider_models.length} />
            <StatCard label="路由" value={data.routes.length} />
          </div>

          <section className="panel">
            <div className="panel-header">
              <h2>TOML 配置</h2>
              <div className="button-row">
                <button className="secondary-button" type="button" disabled={busy} onClick={importToml}>
                  <Upload size={16} />
                  从 TOML 导入
                </button>
                <button className="secondary-button" type="button" disabled={busy} onClick={exportToml}>
                  <Download size={16} />
                  导出 TOML
                </button>
              </div>
            </div>
            {exportText && (
              <label className="field-block">
                <span>导出结果不包含真实 API key</span>
                <textarea value={exportText} readOnly rows={12} />
              </label>
            )}
            {exportText && (
              <div className="drawer-actions">
                <button
                  className="secondary-button"
                  type="button"
                  onClick={async () => {
                    try {
                      await copyText(exportText);
                      notify("已复制 TOML", "success");
                    } catch (error) {
                      notify(errorMessage(error), "error");
                    }
                  }}
                >
                  <Clipboard size={16} />
                  复制
                </button>
              </div>
            )}
          </section>
        </>
      )}
    </div>
  );
}
