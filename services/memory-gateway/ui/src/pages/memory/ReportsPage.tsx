import { useState } from "react";
import {
  Clipboard,
  ClipboardCopy,
  Download,
  ShieldAlert,
  ShieldCheck,
  Upload
} from "lucide-react";
import { MemoryApi } from "../../api";
import type {
  ConnectionSettings,
  MemoryExport,
  MemoryReport,
  RestoreResult,
  StackBackupValidationResult,
  ConversationImportPreviewResult,
  ConversationImportCommitResult
} from "../../types";
import { PageHeader } from "../../components/PageHeader";
import { StatCard } from "../../components/StatCard";
import { ErrorBlock, LoadingBlock } from "../../components/StateBlocks";
import type { ConfirmFn } from "../../hooks/useConfirm";
import { useAsyncData } from "../../hooks/useAsyncData";
import { downloadBlob, downloadFile, copyText } from "../../utils/files";
import { errorMessage, reportSectionTitle } from "../../utils/format";
import type { Notify } from "../pageTypes";

const SOURCE_RESTORE_COMMANDS = `scripts/memgw stack stop
scripts/memgw stack restore /path/to/memory-stack.zip --yes
scripts/memgw stack start`;

const DOCKER_RESTORE_COMMANDS = `# 在安装目录（默认 ~/memory-platform）执行
docker compose -f docker-compose.user.yml cp \\
  ./memory-stack.zip memory-gateway:/data/restore.zip
docker compose -f docker-compose.user.yml stop
docker compose -f docker-compose.user.yml --profile maintenance run --rm \\
  --entrypoint python stack-maintenance \\
  /usr/local/libexec/memory-platform/restore_split.py
docker compose -f docker-compose.user.yml up -d`;

export function ReportsPage({
  api,
  settings,
  notify,
  confirm
}: {
  api: MemoryApi;
  settings: ConnectionSettings;
  notify: Notify;
  confirm: ConfirmFn;
}) {
  const { state, reload: load } = useAsyncData<MemoryReport>(
    (signal) => api.memoryReport(signal),
    [api]
  );
  const [restorePreview, setRestorePreview] = useState<MemoryExport | null>(null);
  const [overwrite, setOverwrite] = useState(false);
  const [includeDeleted, setIncludeDeleted] = useState(false);
  const [restoreResult, setRestoreResult] = useState<RestoreResult | null>(null);
  const [stackAdminKey, setStackAdminKey] = useState("");
  const [stackBusy, setStackBusy] = useState(false);
  const [stackValidateBusy, setStackValidateBusy] = useState(false);
  const [stackValidation, setStackValidation] = useState<StackBackupValidationResult | null>(
    null
  );
  const [stackValidationError, setStackValidationError] = useState<string | null>(null);
  const [importText, setImportText] = useState("");
  const [importPreview, setImportPreview] = useState<ConversationImportPreviewResult | null>(
    null
  );
  const [importResult, setImportResult] = useState<ConversationImportCommitResult | null>(null);
  const [importBusy, setImportBusy] = useState(false);
  const exportUserId = safeExportUserId(settings.userId || "default");

  const copyMarkdown = async () => {
    try {
      const markdown = await api.memoryReportMarkdown();
      await copyText(markdown);
      notify("Markdown 已复制", "success");
    } catch (error) {
      notify(errorMessage(error), "error");
    }
  };

  const exportFile = async (format: "json" | "markdown") => {
    try {
      const data = await api.exportMemories(format);
      if (format === "json") {
        downloadFile(
          `memory-export-${exportUserId}.json`,
          JSON.stringify(data, null, 2),
          "application/json"
        );
      } else {
        downloadFile(`memory-export-${exportUserId}.md`, String(data), "text/markdown");
      }
      notify(`已下载 ${format.toUpperCase()} 导出`, "success");
    } catch (error) {
      notify(errorMessage(error), "error");
    }
  };

  const exportObsidianZip = async () => {
    try {
      const blob = await api.exportObsidianZip();
      downloadBlob(`memory-obsidian-export-${exportUserId}.zip`, blob);
      notify("已下载 Obsidian Zip", "success");
    } catch (error) {
      notify(errorMessage(error), "error");
    }
  };

  const exportStackZip = async () => {
    const confirmed = await confirm({
      title: "下载整栈便携备份",
      message: (
        <span>
          将打包记忆库、知识库、访问令牌哈希、脱敏模型配置等。
          注意这是<strong>整实例</strong>备份：包含本部署<strong>所有用户</strong>的数据，
          不只是当前登录身份。
          <strong>不含</strong>供应商 API Key 与 admin 密钥；换机后需重新填写密钥。
          备份内含明文记忆与知识正文，请当敏感文件保管。
        </span>
      ),
      confirmLabel: "下载备份",
      tone: "warning"
    });
    if (!confirmed) return;
    setStackBusy(true);
    try {
      const blob = await api.exportStackBackup({
        modelGatewayAdminKey: stackAdminKey.trim() || undefined
      });
      const stamp = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
      downloadBlob(`memory-stack-backup-${stamp}.zip`, blob);
      // 备份完成后立即丢弃 admin 密钥明文，不让它留在表单里。
      setStackAdminKey("");
      notify("已下载整栈便携备份（不含密钥）", "success");
    } catch (error) {
      notify(errorMessage(error), "error");
    } finally {
      setStackBusy(false);
    }
  };

  const validateStackZip = async (file: File | null) => {
    setStackValidation(null);
    setStackValidationError(null);
    if (!file) return;
    setStackValidateBusy(true);
    try {
      const result = await api.validateStackBackup(file);
      setStackValidation(result);
      notify(result.ok ? "备份校验通过" : "备份无法恢复", result.ok ? "success" : "error");
    } catch (error) {
      setStackValidationError(errorMessage(error));
      notify(errorMessage(error), "error");
    } finally {
      setStackValidateBusy(false);
    }
  };

  const copyRestoreCommands = async (text: string, label: string) => {
    try {
      await copyText(text);
      notify(`已复制${label}`, "success");
    } catch (error) {
      notify(
        `复制失败：${errorMessage(error)}。请手动选中命令块复制。`,
        "error"
      );
    }
  };

  const runImportPreview = async () => {
    if (!importText.trim()) {
      notify("请粘贴对话导出内容", "error");
      return;
    }
    setImportBusy(true);
    setImportResult(null);
    try {
      const preview = await api.previewConversationImport(importText);
      setImportPreview(preview);
      notify(`解析到 ${preview.turn_count} 轮用户消息`, "success");
    } catch (error) {
      setImportPreview(null);
      notify(errorMessage(error), "error");
    } finally {
      setImportBusy(false);
    }
  };

  const runImportCommit = async () => {
    if (!importText.trim() || !importPreview) return;
    const confirmed = await confirm({
      title: "导入历史对话？",
      message: (
        <span>
          将对 {importPreview.turn_count} 轮用户消息运行与在线聊天相同的提取门控。
          不会自动固定或写入核心记忆；可能产生 LLM 费用。
        </span>
      ),
      confirmLabel: "开始导入",
      tone: "warning"
    });
    if (!confirmed) return;
    setImportBusy(true);
    try {
      const result = await api.commitConversationImport(importText);
      setImportResult(result);
      notify(
        `导入完成：新建 ${result.created}，更新 ${result.updated}，忽略 ${result.ignored}`,
        "success"
      );
    } catch (error) {
      notify(errorMessage(error), "error");
    } finally {
      setImportBusy(false);
    }
  };

  const chooseRestoreFile = async (file: File | null) => {
    setRestoreResult(null);
    if (!file) return;
    try {
      const parsed = JSON.parse(await file.text()) as MemoryExport;
      setRestorePreview(parsed);
      notify("已读取导入文件", "success");
    } catch {
      setRestorePreview(null);
      notify("JSON 文件无法解析", "error");
    }
  };

  const runRestore = async () => {
    if (!restorePreview) return;
    if (
      !(await confirm({
        title: "恢复导入",
        message: (
          <span>
            执行导入前建议先导出备份。
            {overwrite
              ? "已勾选「覆盖已有」，同 ID 的现有记忆将被导入内容更新。"
              : "未勾选「覆盖已有」，同 ID 的现有记忆将保持不变。"}
            确认继续导入？
          </span>
        ),
        tone: "warning",
        confirmLabel: "继续导入"
      }))
    ) {
      return;
    }
    try {
      const result = await api.restoreFromExport(restorePreview, overwrite, includeDeleted);
      setRestoreResult(result);
      notify("导入完成", "success");
      await load();
    } catch (error) {
      notify(errorMessage(error), "error");
    }
  };

  return (
    <div className="page-stack">
      <PageHeader
        title="报告与备份"
        subtitle="导出备份或导入历史对话。"
        showTitle={false}
      />
      <section className="panel">
        <div className="panel-header">
          <h2>记忆报告</h2>
          <button className="secondary-button" type="button" onClick={copyMarkdown}>
            <Clipboard size={16} />
            复制 Markdown
          </button>
        </div>
        {state.loading && <LoadingBlock label="正在加载报告" />}
        {state.error && <ErrorBlock message={state.error} onRetry={() => void load()} />}
        {state.data && (
          <>
            <div className="stats-grid">
              <StatCard label="活跃记忆" value={state.data.counts.active_memories} />
              <StatCard label="回收站记忆" value={state.data.counts.deleted_memories} />
              <StatCard label="核心分区" value={state.data.counts.core_sections} />
            </div>
            <div className="section-list">
              {state.data.sections.map((section) => (
                <article className="section-summary" key={section.section}>
                  <div>
                    <strong>{reportSectionTitle(section.section, section.title)}</strong>
                    <span className="muted"> {section.section}</span>
                  </div>
                  <span>{section.memories.length} 条记忆</span>
                </article>
              ))}
            </div>
          </>
        )}
      </section>

      <section className="panel export-panel">
        <div className="panel-header">
          <h2>导出备份</h2>
        </div>
        <div className="notice warning">
          <ShieldAlert size={18} />
          导出的 JSON、Markdown 和 Obsidian Zip 会包含完整私密/敏感正文。
        </div>
        <div className="button-row">
          <button className="primary-button" type="button" onClick={() => exportFile("json")}>
            <Download size={16} />
            下载 JSON
          </button>
          <button className="secondary-button" type="button" onClick={() => exportFile("markdown")}>
            <Download size={16} />
            下载 Markdown
          </button>
          <button className="secondary-button" type="button" onClick={exportObsidianZip}>
            <Download size={16} />
            下载 Obsidian Zip
          </button>
        </div>
      </section>

      <section className="panel export-panel stack-backup-panel">
        <div className="panel-header">
          <h2>整栈便携备份</h2>
        </div>
        <p className="muted">
          换机或升级前：记忆、知识、令牌哈希和模型路由（不含 API Key）。覆盖本部署所有用户。
        </p>
        <label className="field-block">
          <span>Model Gateway admin 密钥（可选）</span>
          <input
            type="password"
            value={stackAdminKey}
            onChange={(event) => setStackAdminKey(event.target.value)}
            autoComplete="off"
            spellCheck={false}
            placeholder="Docker 拆分部署时填写；同机源码安装通常可留空"
          />
        </label>
        <div className="button-row">
          <button
            className="primary-button"
            type="button"
            disabled={stackBusy}
            onClick={() => void exportStackZip()}
          >
            <Download size={16} />
            {stackBusy ? "正在打包…" : "下载整栈备份 Zip"}
          </button>
        </div>

        <div className="stack-restore-guide">
          <h3>校验备份并准备恢复</h3>
          <p className="muted">
            Console <strong>不会</strong>在线替换运行中的数据库（避免半写与锁冲突）。
            请先校验 zip，再<strong>停服</strong>后用下方命令恢复。密钥不在备份内，恢复后需在目标机重新填写供应商 key / admin key。
          </p>
          <label className="upload-box">
            <ShieldCheck size={18} />
            {stackValidateBusy ? "正在校验…" : "选择备份 Zip 校验"}
            <input
              type="file"
              accept="application/zip,.zip"
              disabled={stackValidateBusy}
              onChange={(event) => {
                const next = event.target.files?.[0] || null;
                event.target.value = "";
                void validateStackZip(next);
              }}
            />
          </label>
          {stackValidationError && (
            <div className="notice warning">
              <ShieldAlert size={18} />
              {stackValidationError}
            </div>
          )}
          {stackValidation && (
            <div className="stack-validation-result">
              <div className="notice success">
                <ShieldCheck size={18} />
                {stackValidation.message || "备份校验通过"}
              </div>
              <div className="result-grid">
                <StatCard label="备份版本" value={stackValidation.version ?? "-"} />
                <StatCard
                  label="记忆用户数"
                  value={stackValidation.stats?.memory_users ?? "-"}
                />
                <StatCard
                  label="活跃记忆"
                  value={stackValidation.stats?.active_memories ?? "-"}
                />
                <StatCard
                  label="知识文档"
                  value={stackValidation.stats?.knowledge_documents ?? "-"}
                />
              </div>
            </div>
          )}

          <details className="restore-advanced">
            <summary>高级：停服后的恢复命令</summary>
          <div className="restore-command-block">
            <div className="panel-header">
              <h4>源码安装恢复</h4>
              <button
                type="button"
                className="secondary-button compact"
                onClick={() => void copyRestoreCommands(SOURCE_RESTORE_COMMANDS, "源码恢复命令")}
              >
                <ClipboardCopy size={14} />
                复制
              </button>
            </div>
            <pre className="command-pre">{SOURCE_RESTORE_COMMANDS}</pre>
          </div>
          <div className="restore-command-block">
            <div className="panel-header">
              <h4>Docker 安装恢复</h4>
              <button
                type="button"
                className="secondary-button compact"
                onClick={() => void copyRestoreCommands(DOCKER_RESTORE_COMMANDS, "Docker 恢复命令")}
              >
                <ClipboardCopy size={14} />
                复制
              </button>
            </div>
            <pre className="command-pre">{DOCKER_RESTORE_COMMANDS}</pre>
          </div>
          <p className="muted">
            完整说明见运维文档「备份、恢复与迁移」。安装目录丢失但卷仍在时，优先重挂载 credentials，不必整栈恢复。
          </p>
          </details>
        </div>
      </section>

      <section className="panel restore-panel conversation-import-panel">
        <div className="panel-header">
          <h2>历史对话导入</h2>
        </div>
        <p className="muted">
          粘贴 Chatbox / OpenAI 风格 messages JSON，或带 <code>User:</code> / <code>Assistant:</code>{" "}
          标记的文本。先预览再提交；每轮走同源提取门控，不直接灌库。
        </p>
        <textarea
          className="conversation-import-input"
          rows={8}
          value={importText}
          onChange={(event) => {
            setImportText(event.target.value);
            setImportPreview(null);
            setImportResult(null);
          }}
          placeholder='{"messages":[{"role":"user","content":"我喜欢黑咖啡"},{"role":"assistant","content":"好的"}]}'
        />
        <div className="button-row">
          <button
            type="button"
            className="secondary-button"
            disabled={importBusy || !importText.trim()}
            onClick={() => void runImportPreview()}
          >
            {importBusy ? "处理中…" : "预览解析"}
          </button>
          <button
            type="button"
            className="warning-button"
            disabled={importBusy || !importPreview}
            onClick={() => void runImportCommit()}
          >
            确认导入
          </button>
        </div>
        {importPreview && (
          <div className="import-preview">
            <div className="result-grid">
              <StatCard label="轮数" value={importPreview.turn_count} />
              <StatCard label="格式" value={importPreview.format} />
              <StatCard label="字符" value={importPreview.total_chars} />
            </div>
            {importPreview.warnings.length > 0 && (
              <p className="muted">提示：{importPreview.warnings.join("；")}</p>
            )}
            <ul className="import-sample-list">
              {importPreview.sample_turns.map((turn) => (
                <li key={turn.index}>
                  <strong>#{turn.index + 1}</strong> {turn.user_text}
                </li>
              ))}
            </ul>
          </div>
        )}
        {importResult && (
          <div className="result-grid">
            <StatCard label="新建" value={importResult.created} />
            <StatCard label="更新" value={importResult.updated} />
            <StatCard label="忽略" value={importResult.ignored} />
            <StatCard label="批次" value={importResult.batch_id} />
          </div>
        )}
      </section>

      <section className="panel restore-panel">
        <div className="panel-header">
          <h2>记忆 JSON 恢复导入</h2>
        </div>
        <div className="notice warning">
          <ShieldAlert size={18} />
          执行导入前建议先导出备份。
        </div>
        <label className="upload-box">
          <Upload size={18} />
          上传 JSON
          <input
            type="file"
            accept="application/json,.json"
            onChange={(event) => void chooseRestoreFile(event.target.files?.[0] || null)}
          />
        </label>
        {restorePreview && (
          <div className="restore-preview">
            <StatCard label="活跃记忆" value={restorePreview.memories?.length || 0} />
            <StatCard label="回收站记忆" value={restorePreview.deleted_memories?.length || 0} />
            <StatCard label="核心分区" value={restorePreview.core_memory_sections?.length || 0} />
          </div>
        )}
        <div className="button-row">
          <label className="checkbox-row">
            <input
              type="checkbox"
              checked={overwrite}
              onChange={(event) => setOverwrite(event.target.checked)}
            />
            覆盖已有
          </label>
          <label className="checkbox-row">
            <input
              type="checkbox"
              checked={includeDeleted}
              onChange={(event) => setIncludeDeleted(event.target.checked)}
            />
            包含回收站
          </label>
          <button
            className="warning-button"
            type="button"
            disabled={!restorePreview}
            onClick={runRestore}
          >
            确认导入
          </button>
        </div>
        {restoreResult && (
          <div className="result-grid">
            <StatCard label="新增" value={restoreResult.created} />
            <StatCard label="更新" value={restoreResult.updated} />
            <StatCard label="跳过" value={restoreResult.skipped} />
            <StatCard label="无效" value={restoreResult.invalid} />
          </div>
        )}
      </section>
    </div>
  );
}

function safeExportUserId(value: string): string {
  const cleaned = Array.from(value)
    .map((character) =>
      /^[A-Za-z0-9_-]$/.test(character) ? character : "-"
    )
    .join("")
    .replace(/[-_]+$/g, "")
    .replace(/^[-_]+/g, "");
  return cleaned.slice(0, 80) || "default";
}



