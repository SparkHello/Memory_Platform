import {
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  Eye,
  EyeOff,
  KeyRound,
  LockKeyhole,
  Plus,
  PlugZap,
  RefreshCcw,
  Save,
  ShieldCheck,
  TriangleAlert,
  XCircle
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { isAbortError, type MemoryApi } from "../../api";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { PageHeader } from "../../components/PageHeader";
import { EmptyBlock, ErrorBlock, LoadingBlock } from "../../components/StateBlocks";
import { useConfirm, type ConfirmFn } from "../../hooks/useConfirm";
import type {
  ModelGatewayConnectionCheck,
  ModelGatewayConnectionInfo,
  ModelGatewayControlSnapshot,
  ModelGatewayDeploymentInfo,
  ModelGatewayRouteDraft,
  ModelGatewayRouteInfo,
  ProviderInfo,
  ProvidersStatus,
  RouteInfo
} from "../../types";
import { errorMessage } from "../../utils/format";
import { NewChannelWizard, ROUTE_LABELS } from "./NewChannelWizard";

type Feedback = { tone: "success" | "warning" | "error"; message: string };
type ConnectionCheckState = "checking" | ModelGatewayConnectionCheck;

// 未保存修改保护：dirty 时拦截刷新/关闭和站内导航点击，确认后才放行。
// 站内导航由 App 先改 state 再改 hash，hashchange 触发时本页已卸载，
// 只能在捕获阶段拦截导航控件的点击，确认后重新触发原按钮完成跳转。
function useUnsavedChangesGuard(dirty: boolean, message: string, confirm: ConfirmFn) {
  const allowNextClickRef = useRef(false);
  const dirtyRef = useRef(dirty);
  dirtyRef.current = dirty;

  useEffect(() => {
    if (!dirty) return;
    const onBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    const onClickCapture = (event: MouseEvent) => {
      if (allowNextClickRef.current) {
        allowNextClickRef.current = false;
        return;
      }
      if (!dirtyRef.current) return;
      const target = event.target instanceof Element ? event.target : null;
      const button = target?.closest<HTMLElement>(
        ".sidebar .nav-item, .mobile-bottom-nav button:not(:last-child), .mobile-more-grid button, .avatar-chip"
      );
      if (!button || button.classList.contains("active") || button.getAttribute("aria-current") === "page") {
        return;
      }
      event.preventDefault();
      event.stopPropagation();
      void confirm({
        title: "离开当前页面？",
        message,
        confirmLabel: "放弃修改并离开",
        cancelLabel: "继续编辑",
        tone: "warning"
      }).then((confirmed) => {
        if (confirmed) {
          allowNextClickRef.current = true;
          button.click();
        }
      });
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    document.addEventListener("click", onClickCapture, true);
    return () => {
      window.removeEventListener("beforeunload", onBeforeUnload);
      document.removeEventListener("click", onClickCapture, true);
    };
  }, [dirty, message, confirm]);
}

export function ProvidersPage({ api }: { api: MemoryApi }) {
  const [status, setStatus] = useState<ProvidersStatus | null>(null);
  const [drafts, setDrafts] = useState<ModelGatewayRouteDraft[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [adminKey, setAdminKey] = useState("");
  const [showAdminKey, setShowAdminKey] = useState(false);
  const [secretValues, setSecretValues] = useState<Record<string, string>>({});
  const [showSecrets, setShowSecrets] = useState<Record<string, boolean>>({});
  const [checks, setChecks] = useState<Record<string, ConnectionCheckState>>({});
  const [busyAction, setBusyAction] = useState("");
  const [feedback, setFeedback] = useState<Feedback | null>(null);
  const [validatedSignature, setValidatedSignature] = useState("");
  const [wizardOpen, setWizardOpen] = useState(false);
  const { confirm, confirmState, resolveConfirm } = useConfirm();

  const load = useCallback(
    async (signal?: AbortSignal, preserveDrafts = false) => {
      setLoading(true);
      setError(null);
      try {
        const next = await api.providersStatus(signal);
        setStatus(next);
        if (!preserveDrafts) {
          setDrafts(routeDrafts(next.control));
          setValidatedSignature("");
        }
      } catch (cause) {
        if (isAbortError(cause)) return;
        setError(errorMessage(cause));
      } finally {
        setLoading(false);
      }
    },
    [api]
  );

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const baselineSignature = useMemo(
    () => JSON.stringify(routeDrafts(status?.control || null)),
    [status?.control]
  );
  const draftSignature = useMemo(() => JSON.stringify(drafts), [drafts]);
  const dirty = Boolean(status?.control) && draftSignature !== baselineSignature;
  const hasAdminKey = Boolean(adminKey.trim());
  const validated = dirty && validatedSignature === draftSignature;
  useUnsavedChangesGuard(dirty, "路由草稿尚未应用，离开后这些修改会丢失。确定要离开吗？", confirm);

  const updateDrafts = (updater: (current: ModelGatewayRouteDraft[]) => ModelGatewayRouteDraft[]) => {
    setDrafts((current) => updater(current));
    setValidatedSignature("");
    setFeedback(null);
  };

  const moveTarget = (routeId: string, index: number, direction: -1 | 1) => {
    updateDrafts((current) =>
      current.map((route) => {
        if (route.id !== routeId) return route;
        const nextIndex = index + direction;
        if (nextIndex < 0 || nextIndex >= route.targets.length) return route;
        const targets = [...route.targets];
        [targets[index], targets[nextIndex]] = [targets[nextIndex], targets[index]];
        return { ...route, targets };
      })
    );
  };

  const toggleRoute = (routeId: string, enabled: boolean) => {
    updateDrafts((current) =>
      current.map((route) => (route.id === routeId ? { ...route, enabled } : route))
    );
  };

  const validateRoutes = async () => {
    if (!status?.control || !dirty || !hasAdminKey) return;
    setBusyAction("validate");
    setFeedback(null);
    try {
      const result = await api.validateProviderRoutes(
        status.control.revision,
        drafts,
        adminKey.trim()
      );
      setValidatedSignature(draftSignature);
      const warning = result.warnings[0];
      setFeedback({
        tone: warning ? "warning" : "success",
        message: warning || `校验通过：${result.changed_routes.length} 条用途路由可以安全应用。`
      });
    } catch (cause) {
      setValidatedSignature("");
      setFeedback({ tone: "error", message: errorMessage(cause) });
    } finally {
      setBusyAction("");
    }
  };

  const applyRoutes = async () => {
    if (!status?.control || !validated || !hasAdminKey) return;
    setBusyAction("apply");
    setFeedback(null);
    try {
      const result = await api.applyProviderRoutes(
        status.control.revision,
        drafts,
        adminKey.trim()
      );
      await load(undefined, false);
      setFeedback({
        tone: "success",
        message: `已应用 ${result.changed_routes.length} 条用途路由，Model Gateway 已热加载，无需重启。`
      });
    } catch (cause) {
      setValidatedSignature("");
      setFeedback({ tone: "error", message: errorMessage(cause) });
    } finally {
      setBusyAction("");
    }
  };

  const resetDrafts = () => {
    setDrafts(routeDrafts(status?.control || null));
    setValidatedSignature("");
    setFeedback(null);
  };

  const saveSecret = async (connection: ModelGatewayConnectionInfo) => {
    const value = secretValues[connection.id] || "";
    if (!hasAdminKey || !value) return;
    setBusyAction(`secret:${connection.id}`);
    setFeedback(null);
    try {
      await api.updateProviderSecret(connection.id, value, adminKey.trim());
      setSecretValues((current) => ({ ...current, [connection.id]: "" }));
      await load(undefined, true);
      setFeedback({
        tone: "success",
        message: `${connection.channel_operator} 的密钥已替换。密钥值未被页面读取或保存。`
      });
    } catch (cause) {
      setFeedback({ tone: "error", message: errorMessage(cause) });
    } finally {
      setBusyAction("");
    }
  };

  const checkConnection = async (connection: ModelGatewayConnectionInfo) => {
    if (!hasAdminKey) return;
    setChecks((current) => ({ ...current, [connection.id]: "checking" }));
    try {
      const result = await api.checkProviderConnection(connection.id, adminKey.trim());
      setChecks((current) => ({ ...current, [connection.id]: result }));
    } catch (cause) {
      setChecks((current) => {
        const next = { ...current };
        delete next[connection.id];
        return next;
      });
      setFeedback({ tone: "error", message: errorMessage(cause) });
    }
  };

  return (
    <div className="page-stack providers-page">
      <PageHeader
        title="模型与路由"
        subtitle="管理模型渠道密钥和每项用途的故障切换顺序；密钥只单向写入 Model Gateway。"
        action={
          <div className="provider-page-actions">
            {dirty && (
              <button type="button" className="secondary-button" onClick={resetDrafts}>
                放弃草稿
              </button>
            )}
            <button
              type="button"
              className="secondary-button"
              onClick={() => void load(undefined, false)}
              disabled={loading || Boolean(busyAction)}
            >
              <RefreshCcw size={16} aria-hidden />
              {dirty ? "放弃并刷新" : "刷新"}
            </button>
            {status?.control && (
              <>
                <button
                  type="button"
                  className="secondary-button"
                  onClick={() => void validateRoutes()}
                  disabled={!dirty || !hasAdminKey || Boolean(busyAction)}
                >
                  <ShieldCheck size={16} aria-hidden />
                  {busyAction === "validate" ? "正在校验" : "校验草稿"}
                </button>
                <button
                  type="button"
                  className="primary-button"
                  onClick={() => void applyRoutes()}
                  disabled={!validated || !hasAdminKey || Boolean(busyAction)}
                >
                  <Save size={16} aria-hidden />
                  {busyAction === "apply" ? "正在应用" : "应用配置"}
                </button>
              </>
            )}
          </div>
        }
      />

      {loading && !status && <LoadingBlock label="正在读取模型配置" />}
      {error && <ErrorBlock message={error} onRetry={() => void load()} />}

      {status && (
        <>
          {status.config_error && <ErrorBlock message={`配置不可用：${status.config_error}`} />}
          <RuntimeBanner status={status} dirty={dirty} validated={validated} />

          {feedback && (
            <div className={`provider-feedback is-${feedback.tone}`} role={feedback.tone === "error" ? "alert" : "status"}>
              {feedback.tone === "success" ? (
                <CheckCircle2 size={18} aria-hidden />
              ) : (
                <TriangleAlert size={18} aria-hidden />
              )}
              <span>{feedback.message}</span>
            </div>
          )}

          {status.control ? (
            <>
              <AdminAccess
                value={adminKey}
                show={showAdminKey}
                onChange={(value) => {
                  setAdminKey(value);
                  setValidatedSignature("");
                }}
                onToggleShow={() => setShowAdminKey((current) => !current)}
              />
              <ConnectionsEditor
                control={status.control}
                adminReady={hasAdminKey}
                secretValues={secretValues}
                showSecrets={showSecrets}
                checks={checks}
                busyAction={busyAction}
                onSecretChange={(id, value) =>
                  setSecretValues((current) => ({ ...current, [id]: value }))
                }
                onToggleSecret={(id) =>
                  setShowSecrets((current) => ({ ...current, [id]: !current[id] }))
                }
                onSaveSecret={(connection) => void saveSecret(connection)}
                onCheck={(connection) => void checkConnection(connection)}
                onCreateChannel={() => setWizardOpen(true)}
              />
              {wizardOpen && (
                <NewChannelWizard
                  api={api}
                  adminKey={adminKey}
                  control={status.control}
                  onClose={() => setWizardOpen(false)}
                  onCompleted={() => load(undefined, false)}
                />
              )}
              <RoutesEditor
                control={status.control}
                drafts={drafts}
                onMove={moveTarget}
                onToggle={toggleRoute}
              />
            </>
          ) : (
            <ReadOnlyDirectStatus status={status} />
          )}
        </>
      )}
      <ConfirmDialog state={confirmState} onResolve={resolveConfirm} />
    </div>
  );
}

function AdminAccess({
  value,
  show,
  onChange,
  onToggleShow
}: {
  value: string;
  show: boolean;
  onChange: (value: string) => void;
  onToggleShow: () => void;
}) {
  return (
    <section className="panel provider-admin-access" aria-labelledby="provider-admin-title">
      <div className="provider-admin-copy">
        <span className="provider-admin-icon" aria-hidden>
          <LockKeyhole size={18} />
        </span>
        <div>
          <h2 id="provider-admin-title">解锁本次配置操作</h2>
          <p>管理密钥只保留在当前页面内存中，刷新或关闭页面后自动清除。</p>
        </div>
      </div>
      <label className="field-block provider-admin-field">
        <span>Model Gateway admin 密钥</span>
        <div className="secret-field">
          <input
            type={show ? "text" : "password"}
            value={value}
            onChange={(event) => onChange(event.target.value)}
            autoComplete="off"
            spellCheck={false}
            placeholder="仅在校验、应用或替换密钥时发送"
          />
          <button
            type="button"
            className="icon-button"
            onClick={onToggleShow}
            aria-label={show ? "隐藏管理密钥" : "显示管理密钥"}
            title={show ? "隐藏管理密钥" : "显示管理密钥"}
          >
            {show ? <EyeOff size={16} aria-hidden /> : <Eye size={16} aria-hidden />}
          </button>
        </div>
      </label>
      <details className="provider-bootstrap-help">
        <summary>还没有 admin 密钥？</summary>
        <p>
          首次运行 <code>memgw stack install</code>（含 <code>scripts/setup.sh</code> 和容器首启）时会自动生成并打印一次；请从当时的终端或容器日志中查找。
        </p>
        <p>丢失后在 Model Gateway 终端重新设置：</p>
        <code>modelgw secret set memory-console-admin</code>
      </details>
    </section>
  );
}

function ConnectionsEditor({
  control,
  adminReady,
  secretValues,
  showSecrets,
  checks,
  busyAction,
  onSecretChange,
  onToggleSecret,
  onSaveSecret,
  onCheck,
  onCreateChannel
}: {
  control: ModelGatewayControlSnapshot;
  adminReady: boolean;
  secretValues: Record<string, string>;
  showSecrets: Record<string, boolean>;
  checks: Record<string, ConnectionCheckState>;
  busyAction: string;
  onSecretChange: (id: string, value: string) => void;
  onToggleSecret: (id: string) => void;
  onSaveSecret: (connection: ModelGatewayConnectionInfo) => void;
  onCheck: (connection: ModelGatewayConnectionInfo) => void;
  onCreateChannel: () => void;
}) {
  const deploymentsByConnection = useMemo(() => {
    const grouped: Record<string, ModelGatewayDeploymentInfo[]> = {};
    for (const deployment of control.deployments) {
      (grouped[deployment.connection] ||= []).push(deployment);
    }
    return grouped;
  }, [control.deployments]);

  return (
    <section className="panel provider-editor-section" aria-labelledby="provider-connections-title">
      <div className="panel-header provider-section-header">
        <div>
          <h2 id="provider-connections-title">模型渠道</h2>
          <p>替换已有渠道密钥并执行免费的模型列表检查；不会发起推理。</p>
        </div>
        <div className="provider-section-actions">
          <span className="provider-count">{control.connections.length} 个渠道</span>
          <button type="button" className="secondary-button" onClick={onCreateChannel}>
            <Plus size={16} aria-hidden />
            新建渠道
          </button>
        </div>
      </div>
      {control.connections.length === 0 && (
        <div className="provider-empty-cta">
          <p>
            还没有任何模型渠道。新建第一个渠道后，即可选择聊天模型并把
            memory.* / knowledge.* 用途路由指向它，无需回到终端。
          </p>
          <button type="button" className="primary-button" onClick={onCreateChannel}>
            <Plus size={16} aria-hidden />
            新建第一个渠道
          </button>
        </div>
      )}
      <div className="provider-connection-list">
        {control.connections.map((connection) => {
          const value = secretValues[connection.id] || "";
          const check = checks[connection.id];
          return (
            <article className="provider-connection-row" key={connection.id}>
              <div className="provider-connection-summary">
                <div className="provider-connection-title">
                  <h3>{connection.channel_operator}</h3>
                  <StatusPill
                    ok={connection.configured}
                    okText="密钥已配置"
                    badText="缺少密钥"
                  />
                </div>
                <p>{connection.base_url}</p>
                <div className="provider-deployment-chips" aria-label="关联 deployment">
                  {(deploymentsByConnection[connection.id] || []).map((deployment) => (
                    <span key={deployment.id}>{deployment.id}</span>
                  ))}
                </div>
              </div>
              <div className="provider-secret-actions">
                <label className="field-block">
                  <span>替换渠道密钥</span>
                  <div className="secret-field">
                    <input
                      type={showSecrets[connection.id] ? "text" : "password"}
                      value={value}
                      onChange={(event) => onSecretChange(connection.id, event.target.value)}
                      autoComplete="new-password"
                      spellCheck={false}
                      placeholder="留空表示不修改"
                      disabled={!adminReady || Boolean(busyAction)}
                    />
                    <button
                      type="button"
                      className="icon-button"
                      onClick={() => onToggleSecret(connection.id)}
                      disabled={!adminReady}
                      aria-label={showSecrets[connection.id] ? "隐藏渠道密钥" : "显示渠道密钥"}
                    >
                      {showSecrets[connection.id] ? (
                        <EyeOff size={16} aria-hidden />
                      ) : (
                        <Eye size={16} aria-hidden />
                      )}
                    </button>
                  </div>
                </label>
                <div className="provider-connection-buttons">
                  <button
                    type="button"
                    className="secondary-button"
                    onClick={() => onSaveSecret(connection)}
                    disabled={!adminReady || !value || Boolean(busyAction)}
                  >
                    <KeyRound size={16} aria-hidden />
                    {busyAction === `secret:${connection.id}` ? "正在保存" : "替换密钥"}
                  </button>
                  <button
                    type="button"
                    className="secondary-button"
                    onClick={() => onCheck(connection)}
                    disabled={!adminReady || check === "checking" || Boolean(busyAction)}
                  >
                    <PlugZap size={16} aria-hidden />
                    {check === "checking" ? "正在检查" : "检查连接"}
                  </button>
                </div>
                {check && check !== "checking" && <ConnectionCheckResult result={check} />}
              </div>
            </article>
          );
        })}
      </div>
    </section>
  );
}

function ConnectionCheckResult({ result }: { result: ModelGatewayConnectionCheck }) {
  const check = result.connections[0];
  if (!check) return null;
  return (
    <div className={`provider-check-result is-${check.level}`} role="status">
      {check.level === "ok" ? <CheckCircle2 size={15} aria-hidden /> : <TriangleAlert size={15} aria-hidden />}
      <span>{check.detail}</span>
    </div>
  );
}

function RoutesEditor({
  control,
  drafts,
  onMove,
  onToggle
}: {
  control: ModelGatewayControlSnapshot;
  drafts: ModelGatewayRouteDraft[];
  onMove: (routeId: string, index: number, direction: -1 | 1) => void;
  onToggle: (routeId: string, enabled: boolean) => void;
}) {
  const deploymentById = useMemo(
    () => Object.fromEntries(control.deployments.map((deployment) => [deployment.id, deployment])),
    [control.deployments]
  );
  const connectionById = useMemo(
    () => Object.fromEntries(control.connections.map((connection) => [connection.id, connection])),
    [control.connections]
  );
  const routeById = useMemo(
    () => Object.fromEntries(control.routes.map((route) => [route.id, route])),
    [control.routes]
  );

  return (
    <section className="provider-routes-section" aria-labelledby="provider-routes-title">
      <div className="provider-routes-heading">
        <div>
          <h2 id="provider-routes-title">用途与优先顺序</h2>
          <p>上方模型不可用时，Model Gateway 按顺序尝试下一个 deployment。</p>
        </div>
        <span>拖动替代为明确的上移/下移按钮，键盘和触控都可操作</span>
      </div>
      <div className="provider-route-editor-list">
        {drafts.map((draft) => (
          <EditableRoute
            key={draft.id}
            draft={draft}
            source={routeById[draft.id]}
            deploymentById={deploymentById}
            connectionById={connectionById}
            onMove={onMove}
            onToggle={onToggle}
          />
        ))}
      </div>
    </section>
  );
}

function EditableRoute({
  draft,
  source,
  deploymentById,
  connectionById,
  onMove,
  onToggle
}: {
  draft: ModelGatewayRouteDraft;
  source?: ModelGatewayRouteInfo;
  deploymentById: Record<string, ModelGatewayDeploymentInfo>;
  connectionById: Record<string, ModelGatewayConnectionInfo>;
  onMove: (routeId: string, index: number, direction: -1 | 1) => void;
  onToggle: (routeId: string, enabled: boolean) => void;
}) {
  return (
    <article className={`provider-route-editor${draft.enabled ? "" : " is-disabled"}`}>
      <header>
        <div className="provider-route-identity">
          <div>
            <h3>{ROUTE_LABELS[draft.id] || draft.id}</h3>
            <code>{draft.id}</code>
          </div>
          {source?.kind === "embedding" && (
            <span className="provider-risk-pill">向量空间敏感</span>
          )}
        </div>
        <label className="provider-route-toggle">
          <input
            type="checkbox"
            checked={draft.enabled}
            onChange={(event) => onToggle(draft.id, event.target.checked)}
          />
          <span>{draft.enabled ? "已启用" : "已停用"}</span>
        </label>
      </header>
      <ol className="provider-route-targets">
        {draft.targets.map((targetId, index) => {
          const deployment = deploymentById[targetId];
          const connection = deployment ? connectionById[deployment.connection] : undefined;
          const usable = Boolean(deployment?.enabled && connection?.enabled && connection?.configured);
          return (
            <li key={targetId}>
              <span className="provider-route-position">{index === 0 ? "首选" : `备用 ${index}`}</span>
              <div className="provider-route-target-copy">
                <div>
                  <code>{targetId}</code>
                  <StatusPill ok={usable} okText="可用" badText="需处理" />
                </div>
                <p>
                  {deployment?.upstream_model || "deployment 不存在"}
                  {connection ? ` · ${connection.channel_operator}` : ""}
                </p>
                {deployment?.kind === "embedding" && (
                  <small>
                    {deployment.dimensions} 维 · {deployment.embedding_space}
                  </small>
                )}
              </div>
              <div className="provider-route-move-actions">
                <button
                  type="button"
                  className="icon-button"
                  onClick={() => onMove(draft.id, index, -1)}
                  disabled={index === 0}
                  aria-label={`将 ${targetId} 上移`}
                  title="上移"
                >
                  <ChevronUp size={17} aria-hidden />
                </button>
                <button
                  type="button"
                  className="icon-button"
                  onClick={() => onMove(draft.id, index, 1)}
                  disabled={index === draft.targets.length - 1}
                  aria-label={`将 ${targetId} 下移`}
                  title="下移"
                >
                  <ChevronDown size={17} aria-hidden />
                </button>
              </div>
            </li>
          );
        })}
      </ol>
      {source && (
        <footer>
          <span>最多尝试 {source.max_attempts} 次</span>
          {source.required_capabilities.map((capability) => (
            <span key={capability}>{capability}</span>
          ))}
        </footer>
      )}
    </article>
  );
}

function RuntimeBanner({
  status,
  dirty,
  validated
}: {
  status: ProvidersStatus;
  dirty: boolean;
  validated: boolean;
}) {
  const { runtime, embedding } = status;
  return (
    <div className="runtime-banner">
      <div>
        <strong>配置权威</strong>
        <span>{runtime.model_gateway_enabled ? "独立 Model Gateway" : "本项目兼容配置"}</span>
      </div>
      <div>
        <strong>内部接线</strong>
        <span>
          {runtime.model_gateway_base_url
            ? `${runtime.model_gateway_base_url}（仅服务间通信，不用填进客户端）`
            : "直连各供应商"}
        </span>
      </div>
      <div>
        <strong>客户端接入</strong>
        <span>{window.location.origin}/v1 · 模型 memory-auto</span>
      </div>
      <div>
        <strong>向量模型</strong>
        <span>
          {embedding.model} · {embedding.dimensions} 维 · {embedding.configured ? "可用" : "未配置"}
        </span>
      </div>
      <div>
        <strong>页面状态</strong>
        <span>{validated ? "草稿已校验" : dirty ? "有未应用草稿" : "与运行配置一致"}</span>
      </div>
    </div>
  );
}

function ReadOnlyDirectStatus({ status }: { status: ProvidersStatus }) {
  return (
    <>
      <div className="provider-feedback is-warning" role="status">
        <TriangleAlert size={18} aria-hidden />
        <span>
          当前处于项目内 direct-provider 兼容模式。此模式继续只读；连接独立 Model Gateway 后才开放安全写入。
        </span>
      </div>
      <h2 className="section-title">供应商</h2>
      {status.providers.length === 0 ? (
        <EmptyBlock label="没有任何供应商定义" hint="请先通过 memgw 或 Model Gateway 完成初始化。" />
      ) : (
        <div className="provider-grid">
          {status.providers.map((provider) => (
            <ProviderCard key={provider.id} provider={provider} />
          ))}
        </div>
      )}
      <h2 className="section-title">功能路由</h2>
      <div className="route-list">
        {status.routes.map((route) => (
          <RouteRow key={route.id} route={route} />
        ))}
      </div>
    </>
  );
}

function ProviderCard({ provider }: { provider: ProviderInfo }) {
  return (
    <article className={`provider-card${provider.configured ? "" : " is-unconfigured"}`}>
      <header>
        <h3>{provider.name}</h3>
        <StatusPill ok={provider.configured} okText="已配置密钥" badText="缺少密钥" />
      </header>
      <p className="provider-host">{provider.api_host}</p>
      <p className="provider-models">
        {provider.models.map((model) => (
          <span className="provider-models-label" key={`${model.kind}:${model.id}`}>
            {model.id}
          </span>
        ))}
      </p>
    </article>
  );
}

function RouteRow({ route }: { route: RouteInfo }) {
  return (
    <div className={`route-row${route.usable ? "" : " is-unusable"}`}>
      <div className="route-row-head">
        <code>{route.id}</code>
        <span className="route-desc">{route.description}</span>
        <StatusPill ok={route.usable} okText="可用" badText="不可用" />
      </div>
      {route.targets.length === 0 ? (
        <p className="route-empty">尚未配置任何模型</p>
      ) : (
        <ol className="route-chain">
          {route.targets.map((target, index) => (
            <li key={target.target} className={target.configured ? "" : "is-unconfigured"}>
              <span className="route-order">{index === 0 ? "首选" : `备${index}`}</span>
              <code>{target.target}</code>
              {!target.valid && <em>目标不存在</em>}
              {target.valid && !target.configured && <em>缺少密钥</em>}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

function StatusPill({ ok, okText, badText }: { ok: boolean; okText: string; badText: string }) {
  return (
    <span className={`status-pill${ok ? " is-ok" : " is-bad"}`}>
      {ok ? <CheckCircle2 size={14} aria-hidden /> : <XCircle size={14} aria-hidden />}
      {ok ? okText : badText}
    </span>
  );
}

function routeDrafts(control: ModelGatewayControlSnapshot | null): ModelGatewayRouteDraft[] {
  return (control?.routes || []).map((route) => ({
    id: route.id,
    targets: [...route.targets],
    enabled: route.enabled
  }));
}
