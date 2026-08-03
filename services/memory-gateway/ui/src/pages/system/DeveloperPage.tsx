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

export function DeveloperPage({
  settings,
  notify
}: {
  settings: ConnectionSettings;
  notify: Notify;
}) {
  const mcpUrl = joinUrl(settings.apiBaseUrl, "/mcp");
  const headers = `Authorization: Bearer ${settings.apiKey}\nX-User-Id: ${settings.userId}`;
  const endpoints = [
    "GET /health",
    "GET /memories",
    "POST /memories/search",
    "GET /memories/core",
    "POST /memories/review",
    "GET /memories/export",
    "GET /knowledge/documents",
    "POST /knowledge/uploads",
    "POST /knowledge/search",
    "POST /knowledge/read",
    "GET /knowledge/export"
  ];

  const copy = async (text: string) => {
    await copyText(text);
    notify("已复制", "success");
  };

  return (
    <div className="page-stack">
      <PageHeader title="接入信息" subtitle="MCP、记忆管理与独立知识库的常用 REST 接入信息。" />
      <section className="panel access-card">
        <div className="panel-header">
          <h2>MCP</h2>
          <button className="secondary-button" type="button" onClick={() => copy(`${mcpUrl}\n${headers}`)}>
            <Clipboard size={16} />
            复制
          </button>
        </div>
        <FieldList entries={[["地址", mcpUrl], ["请求头", headers]]} />
      </section>

      <section className="panel access-card">
        <div className="panel-header">
          <h2>REST</h2>
          <button className="secondary-button" type="button" onClick={() => copy(endpoints.join("\n"))}>
            <Clipboard size={16} />
            复制端点
          </button>
        </div>
        <div className="endpoint-list">
          {endpoints.map((endpoint) => (
            <code key={endpoint}>{endpoint}</code>
          ))}
        </div>
      </section>
    </div>
  );
}


