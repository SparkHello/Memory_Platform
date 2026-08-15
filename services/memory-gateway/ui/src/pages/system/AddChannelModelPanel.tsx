import { CheckCircle2, Plus, Save, ShieldCheck, TriangleAlert, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import type { MemoryApi } from "../../api";
import type {
  ModelGatewayCapabilities,
  ModelGatewayConnectionInfo,
  ModelGatewayControlSnapshot,
  ModelGatewayRouteAssignmentInput
} from "../../types";
import { filterDiscoveredChatModels } from "../../utils/discoveredModels";
import { errorMessage } from "../../utils/format";
import { CAPABILITY_OPTIONS, CHAT_ROUTE_IDS, type ProviderFeedback } from "./providerShared";

export function AddChannelModelPanel({
  api,
  adminKey,
  control,
  connection,
  onClose,
  onCompleted
}: {
  api: MemoryApi;
  adminKey: string;
  control: ModelGatewayControlSnapshot;
  connection: ModelGatewayConnectionInfo;
  onClose: () => void;
  onCompleted: () => Promise<void> | void;
}) {
  const existingModels = useMemo(
    () =>
      new Set(
        control.deployments
          .filter((deployment) => deployment.connection === connection.id && deployment.kind === "chat")
          .map((deployment) => deployment.upstream_model)
      ),
    [control.deployments, connection.id]
  );
  const [models, setModels] = useState<string[]>([]);
  const [chatModel, setChatModel] = useState("");
  const [modelQuery, setModelQuery] = useState("");
  const [adapterProfile, setAdapterProfile] = useState<"inherit" | "dashscope_deepseek_v4">("inherit");
  const [capabilities, setCapabilities] = useState<ModelGatewayCapabilities>({});
  const [validated, setValidated] = useState(false);
  const [busy, setBusy] = useState<"" | "discover" | "validate" | "apply">("");
  const [feedback, setFeedback] = useState<ProviderFeedback | null>(null);
  const [done, setDone] = useState(false);

  const visibleModels = useMemo(() => {
    const unused = models
      .filter((id) => !existingModels.has(id))
      .map((id) => ({ id }));
    return filterDiscoveredChatModels(unused, modelQuery).map((model) => model.id);
  }, [models, existingModels, modelQuery]);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      setBusy("discover");
      setFeedback(null);
      try {
        const report = await api.checkProviderConnection(connection.id, adminKey);
        const row = report.connections[0];
        const discovered = row?.discovered_models || [];
        if (cancelled) return;
        setModels(discovered);
        const unusedChat = filterDiscoveredChatModels(
          discovered.filter((id) => !existingModels.has(id)).map((id) => ({ id }))
        );
        if (unusedChat.length === 1) setChatModel(unusedChat[0].id);
        setFeedback({
          tone: row?.level === "ok" ? "success" : "warning",
          message: discovered.length
            ? `已用该渠道保存的密钥列出 ${discovered.length} 个模型，尚未改路由。`
            : row?.detail || "没有返回模型列表，可以手填精确模型 ID。"
        });
      } catch (cause) {
        if (cancelled) return;
        setFeedback({
          tone: "error",
          message: `${errorMessage(cause, { credential: "admin" })}；未改任何配置。`
        });
      } finally {
        if (!cancelled) setBusy("");
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
    // 只在打开面板时发现一次；保存成功后 existingModels 变化不应重跑。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [adminKey, api, connection.id]);

  const buildRoutes = (): ModelGatewayRouteAssignmentInput[] => {
    return CHAT_ROUTE_IDS.map((id) => {
      const current = control.routes.find((route) => route.id === id);
      const existing = current?.targets || [];
      return {
        id,
        kind: "chat" as const,
        targets: [...existing, "$0"],
        fallback_scope:
          current?.fallback_scope && current.fallback_scope !== "none"
            ? current.fallback_scope
            : "same_channel",
        max_attempts: Math.max(current?.max_attempts || 1, existing.length + 1),
        enabled: current?.enabled ?? true
      };
    });
  };

  const buildBody = (dryRun: boolean) => ({
    revision: control.revision,
    connection: connection.id,
    dry_run: dryRun,
    deployments: [
      {
        upstream_model: chatModel.trim(),
        model_author: "unknown",
        kind: "chat" as const,
        adapter_profile: adapterProfile,
        capabilities: { streaming: true, ...capabilities }
      }
    ],
    routes: buildRoutes()
  });

  const validate = async () => {
    if (!chatModel.trim() || busy) return;
    setBusy("validate");
    setFeedback(null);
    try {
      const result = await api.applyProviderDeployments(buildBody(true), adminKey);
      setValidated(true);
      setFeedback({
        tone: "success",
        message: `检查通过：将新增 1 个聊天模型，并把它追加到 ${result.changed_routes.length || CHAT_ROUTE_IDS.length} 条文字路由的备用顺序。尚未保存。`
      });
    } catch (cause) {
      setValidated(false);
      setFeedback({
        tone: "error",
        message: `${errorMessage(cause, { credential: "admin" })}；现有渠道未改。`
      });
    } finally {
      setBusy("");
    }
  };

  const apply = async () => {
    if (!validated || busy) return;
    setBusy("apply");
    setFeedback(null);
    try {
      const result = await api.applyProviderDeployments(buildBody(false), adminKey);
      await onCompleted();
      setDone(true);
      setFeedback({
        tone: "success",
        message: `已添加 ${result.deployments[0]?.upstream_model || chatModel}，并追加到 ${result.changed_routes.length} 条用途的备用顺序。客户端仍使用 memory-auto。`
      });
    } catch (cause) {
      setValidated(false);
      setFeedback({
        tone: "error",
        message: `${errorMessage(cause, { credential: "admin" })}；请刷新后重试，不要重复提交。`
      });
    } finally {
      setBusy("");
    }
  };

  return (
    <section className="panel provider-editor-section provider-wizard" aria-labelledby="add-model-title">
      <div className="panel-header provider-section-header">
        <div>
          <h2 id="add-model-title">给「{connection.channel_operator}」添加模型</h2>
          <p>复用已保存的渠道密钥和 {connection.base_url}。新模型默认排在现有模型后面作备用。</p>
        </div>
        <button
          type="button"
          className="icon-button"
          onClick={onClose}
          disabled={busy === "apply"}
          aria-label="关闭添加模型"
        >
          <X size={16} aria-hidden />
        </button>
      </div>
      {feedback && (
        <div className={`provider-feedback is-${feedback.tone}`} role={feedback.tone === "error" ? "alert" : "status"}>
          {feedback.tone === "success" ? <CheckCircle2 size={18} aria-hidden /> : <TriangleAlert size={18} aria-hidden />}
          <span>{feedback.message}</span>
        </div>
      )}
      {done ? (
        <div className="provider-wizard-actions">
          <button type="button" className="primary-button" onClick={onClose}>
            完成
          </button>
        </div>
      ) : (
        <>
          <label className="field-block">
            <span>聊天模型</span>
            {models.length > 8 && (
              <input
                value={modelQuery}
                onChange={(event) => setModelQuery(event.target.value)}
                spellCheck={false}
                placeholder="输入以筛选模型 ID"
                aria-label="过滤聊天模型"
                disabled={Boolean(busy)}
              />
            )}
            {models.length > 0 ? (
              <select
                value={chatModel}
                aria-label="聊天模型"
                disabled={Boolean(busy)}
                onChange={(event) => {
                  setChatModel(event.target.value);
                  setValidated(false);
                }}
              >
                <option value="">请选择一个尚未添加的模型</option>
                {visibleModels.map((id) => (
                  <option key={id} value={id}>
                    {id}
                  </option>
                ))}
              </select>
            ) : (
              <input
                value={chatModel}
                onChange={(event) => {
                  setChatModel(event.target.value);
                  setValidated(false);
                }}
                spellCheck={false}
                placeholder="精确模型 ID"
                aria-label="聊天模型"
                disabled={Boolean(busy)}
              />
            )}
          </label>
          <label className="field-block">
            <span>适配 profile</span>
            <select
              value={adapterProfile}
              aria-label="适配 profile"
              disabled={Boolean(busy)}
              onChange={(event) => {
                setAdapterProfile(event.target.value as "inherit" | "dashscope_deepseek_v4");
                setValidated(false);
              }}
            >
              <option value="inherit">继承渠道适配器</option>
              <option value="dashscope_deepseek_v4">DashScope DeepSeek V4 Flash/Pro</option>
            </select>
          </label>
          <fieldset className="provider-capability-field" disabled={Boolean(busy)}>
            <legend>声明此模型支持的能力</legend>
            <div className="provider-capability-grid">
              {CAPABILITY_OPTIONS.map((option) => (
                <label key={option.key}>
                  <input
                    type="checkbox"
                    checked={Boolean(capabilities[option.key])}
                    onChange={(event) => {
                      setCapabilities((current) => ({ ...current, [option.key]: event.target.checked }));
                      setValidated(false);
                    }}
                  />
                  <span>{option.label}</span>
                </label>
              ))}
            </div>
          </fieldset>
          <p className="provider-wizard-hint">
            未勾选的能力会被路由视为不支持。此步骤不会再创建 chat token，也不会改向量路由。
          </p>
          <div className="provider-wizard-actions">
            <button type="button" className="secondary-button" onClick={() => void validate()} disabled={!chatModel.trim() || Boolean(busy)}>
              <ShieldCheck size={16} aria-hidden />
              {busy === "validate" ? "正在检查" : validated ? "重新检查" : "检查配置"}
            </button>
            <button type="button" className="primary-button" onClick={() => void apply()} disabled={!validated || Boolean(busy)}>
              {busy === "apply" ? <Save size={16} aria-hidden /> : <Plus size={16} aria-hidden />}
              {busy === "apply" ? "正在保存" : "确认添加"}
            </button>
          </div>
        </>
      )}
    </section>
  );
}
