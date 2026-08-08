import { Clipboard } from "lucide-react";
import type { ConnectionSettings } from "../../types";
import { FieldList } from "../../components/FormControls";
import { PageHeader } from "../../components/PageHeader";
import { copyText } from "../../utils/files";
import { joinUrl } from "../../utils/format";
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
  // 常用端点的手写摘录，可能与后端漂移，以实际 /health 路由为准。
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

  const copy = async (text: string, message = "已复制") => {
    await copyText(text);
    notify(message, "success");
  };

  return (
    <div className="page-stack">
      <PageHeader title="接入信息" subtitle="MCP、记忆管理与独立知识库的常用 REST 接入信息。" />
      <section className="panel access-card">
        <div className="panel-header">
          <h2>MCP</h2>
          <button
            className="secondary-button"
            type="button"
            onClick={() => copy(`${mcpUrl}\n${headers}`, "已复制，包含你的 API Key，注意保存")}
          >
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
        <p className="muted">端点为常用摘录，以实际 /health 路由为准。</p>
        <div className="endpoint-list">
          {endpoints.map((endpoint) => (
            <code key={endpoint}>{endpoint}</code>
          ))}
        </div>
      </section>
    </div>
  );
}


