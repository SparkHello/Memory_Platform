import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Activity,
  ArchiveRestore,
  Clipboard,
  Download,
  Eye,
  EyeOff,
  FileText,
  KeyRound,
  Layers3,
  ListChecks,
  Pencil,
  RefreshCcw,
  Save,
  Search,
  ShieldAlert,
  Trash2,
  Upload,
  Wrench,
  X
} from "lucide-react";
import { MemoryApi } from "../../api";
import { normalizeBaseUrl } from "../../storage";
import type {
  ConnectionSettings,
  CoreMemoryHistoryItem,
  CoreMemorySection,
  CoreSectionName,
  DecisionLog,
  MemoryAction,
  MemoryExport,
  MemoryRecord,
  MemoryReport,
  MemorySourceExplanation,
  MemoryStability,
  MemorySensitivity,
  MemoryType,
  PageKey,
  RecentContextSummary,
  RestoreResult,
  ReviewAction,
  ReviewRecommendation,
  ReviewResult
} from "../../types";
import { badge } from "../../components/Badge";
import { FieldList, FilterSelect, RangeFields } from "../../components/FormControls";
import { PageHeader } from "../../components/PageHeader";
import { InfoCard, StatCard } from "../../components/StatCard";
import { EmptyBlock, ErrorBlock, LoadingBlock } from "../../components/StateBlocks";
import type { ConfirmFn } from "../../hooks/useConfirm";
import type { LoadState } from "../../hooks/useAsyncData";
import {
  CONFIG_KEYS,
  CORE_SECTIONS,
  DECISIONS,
  MEMORY_TYPES,
  REVIEW_ACTIONS,
  SENSITIVITIES,
  STABILITIES
} from "../../utils/constants";
import { downloadFile, copyText } from "../../utils/files";
import {
  candidateSummary,
  clampNumber,
  dateText,
  displayText,
  errorMessage,
  joinUrl,
  maskSecret,
  percent,
  prettyJson,
  reportSectionTitle,
  reviewActionText,
  sectionTitle,
  shortId
} from "../../utils/format";
import { editDraftToPayload, memoryToEditDraft } from "../../utils/memory";
import type { MemoryEditDraft, MemoryFilters } from "../../utils/memory";
import type { Notify } from "../pageTypes";

export function SettingsPage({
  settings,
  onSave,
  notify
}: {
  settings: ConnectionSettings;
  onSave: (settings: ConnectionSettings, message?: string) => void;
  notify: Notify;
}) {
  const [form, setForm] = useState(settings);
  const [showKey, setShowKey] = useState(false);
  const [testing, setTesting] = useState(false);

  useEffect(() => {
    setForm(settings);
  }, [settings]);

  const testConnection = async () => {
    setTesting(true);
    try {
      const client = new MemoryApi({
        ...form,
        apiBaseUrl: normalizeBaseUrl(form.apiBaseUrl),
        userId: form.userId || "default"
      });
      await client.health();
      await client.memoryReport();
      notify("连接测试通过", "success");
    } catch (error) {
      notify(errorMessage(error), "error");
    } finally {
      setTesting(false);
    }
  };

  return (
    <div className="page-stack">
      <PageHeader
        title="设置"
        subtitle="本地 UI 连接设置和项目配置说明。"
        action={
          <button className="primary-button" type="button" onClick={() => onSave(form)}>
            保存设置
          </button>
        }
      />
      <section className="panel settings-panel">
        <div className="panel-header">
          <h2>连接设置</h2>
        </div>
        <label className="field-block">
          <span>API 基础地址</span>
          <input
            value={form.apiBaseUrl}
            onChange={(event) => setForm({ ...form, apiBaseUrl: event.target.value })}
            placeholder={window.location.origin}
          />
        </label>
        <label className="field-block">
          <span>网关 API Key</span>
          <div className="secret-field">
            <input
              type={showKey ? "text" : "password"}
              value={form.apiKey}
              onChange={(event) => setForm({ ...form, apiKey: event.target.value })}
              placeholder="GATEWAY_API_KEY"
            />
            <button
              className="icon-button"
              type="button"
              onClick={() => setShowKey(!showKey)}
              title={showKey ? "隐藏 API Key" : "显示 API Key"}
            >
              {showKey ? <EyeOff size={16} /> : <Eye size={16} />}
            </button>
          </div>
        </label>
        <label className="field-block">
          <span>用户 ID</span>
          <input
            value={form.userId}
            onChange={(event) => setForm({ ...form, userId: event.target.value })}
            placeholder="default"
          />
        </label>
        <div className="button-row">
          <button className="primary-button" type="button" onClick={() => onSave(form)}>
            保存到本机浏览器
          </button>
          <button
            className="secondary-button"
            type="button"
            disabled={testing}
            onClick={testConnection}
          >
            测试连接
          </button>
        </div>
      </section>

      <section className="panel">
        <div className="panel-header">
          <h2>项目配置说明</h2>
        </div>
        <div className="notice">
          当前版本设置页只保存 UI 连接信息；服务端 .env 修改将在后续版本实现。
        </div>
        <div className="config-grid">
          {CONFIG_KEYS.map((key) => (
            <code key={key}>{key}</code>
          ))}
        </div>
      </section>
    </div>
  );
}



