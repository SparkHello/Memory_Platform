import {
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  Eye,
  EyeOff,
  KeyRound,
  LockKeyhole,
  Plus,
  Power,
  PlugZap,
  RefreshCcw,
  Save,
  ShieldCheck,
  Trash2,
  TriangleAlert,
  XCircle, Pencil, Sparkles } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { isAbortError, type MemoryApi } from "../../api";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { PageHeader } from "../../components/PageHeader";
import { EmptyBlock, ErrorBlock, LoadingBlock } from "../../components/StateBlocks";
import { useConfirm } from "../../hooks/useConfirm";
import { Modal } from "../../components/Modal";
import { useUnsavedChangesGuard } from "../../hooks/useUnsavedChangesGuard";
import type {
  ModelGatewayCapabilities,
  ModelGatewayConnectionCheck,
  ModelGatewayConnectionInfo,
  ModelGatewayControlSnapshot,
  ModelGatewayDeploymentInfo,
  ModelGatewayPricingInfo,
  ModelGatewayRouteDraft,
  ModelGatewayRouteInfo,
  ProviderInfo,
  ProvidersStatus,
  RouteInfo
} from "../../types";
import { CLIENT_MODEL_ID } from "../../utils/constants";
import { errorMessage } from "../../utils/format";
import {
  clearModelAdminKey,
  loadModelAdminKey,
  saveModelAdminKey
} from "../../utils/adminKeySession";
import { isProviderSetupReady } from "../../utils/providerSetup";
import { AddChannelModelPanel } from "./AddChannelModelPanel";
import { NewChannelWizard } from "./NewChannelWizard";
import { channelOperatorLabel, CAPABILITY_OPTIONS, ROUTE_LABELS, type ProviderFeedback } from "./providerShared";

type ConnectionCheckState = "checking" | ModelGatewayConnectionCheck;

export function ProvidersPage({
  api,
  initialSetup = false,
  expertMode = false,
  onSetupChanged
}: {
  api: MemoryApi;
  initialSetup?: boolean;
  expertMode?: boolean;
  onSetupChanged?: () => Promise<void> | void;
}) {
  const [status, setStatus] = useState<ProvidersStatus | null>(null);
  const [drafts, setDrafts] = useState<ModelGatewayRouteDraft[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [adminKey, setAdminKey] = useState(() => loadModelAdminKey());
  const [showAdminKey, setShowAdminKey] = useState(false);
  const [adminCheck, setAdminCheck] = useState<"idle" | "checking" | "valid" | "invalid">("idle");
  const [secretValues, setSecretValues] = useState<Record<string, string>>({});
  const [showSecrets, setShowSecrets] = useState<Record<string, boolean>>({});
  const [checks, setChecks] = useState<Record<string, ConnectionCheckState>>({});
  const [busyAction, setBusyAction] = useState("");
  const [feedback, setFeedback] = useState<ProviderFeedback | null>(null);
  const [validatedSignature, setValidatedSignature] = useState("");
  const [wizardOpen, setWizardOpen] = useState(false);
  const [addModelConnectionId, setAddModelConnectionId] = useState("");
  const addModelConnection =
    status?.control?.connections.find((item) => item.id === addModelConnectionId) || null;
  const { confirm, confirmState, resolveConfirm } = useConfirm();

  const load = useCallback(
    async (signal?: AbortSignal, preserveDrafts = false) => {
      setLoading(true);
      setError(null);
      try {
        const next = await api.providersStatus(signal);
        setStatus((current) => {
          // 常规 status 响应的 live_probe 为 null；不要让刷新覆盖掉
          // 用户刚手动实测得到的探测结果。
          if (current?.setup?.live_probe && next.setup && next.setup.live_probe == null) {
            return {
              ...next,
              setup: {
                ...next.setup,
                live_probe: current.setup.live_probe,
                upstream_ready: current.setup.upstream_ready
              }
            };
          }
          return next;
        });
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

  const loadAdminControl = useCallback(
    async (key: string, preserveDrafts = false) => {
      const control = await api.providerAdminConfiguration(key);
      setStatus((current) => (current ? { ...current, control } : current));
      if (!preserveDrafts) {
        setDrafts(routeDrafts(control));
        setValidatedSignature("");
      }
      return control;
    },
    [api]
  );

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  // 同标签页内验证过的 admin key 存在 sessionStorage：切换页面后恢复并静默重验，
  // 不必重新粘贴；密钥已轮换（重验失败）时丢弃存档，回到手动输入。
  const restoredAdminKeyRef = useRef(adminKey);
  useEffect(() => {
    const restored = restoredAdminKeyRef.current.trim();
    if (!restored) return;
    let cancelled = false;
    setAdminCheck("checking");
    void (async () => {
      try {
        await api.checkProviderAdminKey(restored);
        await loadAdminControl(restored, false);
        if (!cancelled) setAdminCheck("valid");
      } catch (cause) {
        if (cancelled || isAbortError(cause)) return;
        clearModelAdminKey();
        setAdminCheck("idle");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [api, loadAdminControl]);

  const baselineSignature = useMemo(
    () => JSON.stringify(routeDrafts(status?.control || null)),
    [status?.control]
  );
  const draftSignature = useMemo(() => JSON.stringify(drafts), [drafts]);
  const dirty = Boolean(status?.control) && draftSignature !== baselineSignature;
  const hasAdminKey = adminCheck === "valid";
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
      setFeedback({ tone: "error", message: errorMessage(cause, { credential: "admin" }) });
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
      await loadAdminControl(adminKey.trim(), false);
      setFeedback({
        tone: "success",
        message: `已应用 ${result.changed_routes.length} 条用途路由，Model Gateway 已热加载，无需重启。`
      });
    } catch (cause) {
      setValidatedSignature("");
      setFeedback({ tone: "error", message: errorMessage(cause, { credential: "admin" }) });
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
      await loadAdminControl(adminKey.trim(), true);
      setFeedback({
        tone: "success",
        message: `${connection.channel_operator} 的密钥已替换。密钥值未被页面读取或保存。`
      });
    } catch (cause) {
      setFeedback({ tone: "error", message: errorMessage(cause, { credential: "admin" }) });
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
      setFeedback({ tone: "error", message: errorMessage(cause, { credential: "admin" }) });
    }
  };

  const runLiveProbe = async () => {
    if (busyAction) return;
    setBusyAction("live-probe");
    setFeedback(null);
    try {
      const probe = await api.liveUpstreamProbe();
      setStatus((current) =>
        current
          ? {
              ...current,
              setup: {
                ...current.setup,
                live_probe: probe,
                upstream_ready: probe.ok
              }
            }
          : current
      );
      setFeedback({
        tone: probe.ok ? "success" : "error",
        message: probe.ok
          ? `上游可达（${probe.latency_ms ?? "?"} ms · ${probe.route || "chat"}）`
          : `上游探测失败：${probe.message || probe.code}`
      });
    } catch (cause) {
      setFeedback({ tone: "error", message: errorMessage(cause) });
    } finally {
      setBusyAction("");
    }
  };

  const checkAdminKey = async () => {
    if (!adminKey.trim() || adminCheck === "checking") return;
    setAdminCheck("checking");
    setFeedback(null);
    try {
      await api.checkProviderAdminKey(adminKey.trim());
      const fullControl = await loadAdminControl(adminKey.trim(), false);
      setAdminCheck("valid");
      saveModelAdminKey(adminKey.trim());
      setFeedback({ tone: "success", message: "管理密钥验证成功，可以继续配置模型。" });
      if (initialSetup && fullControl.connections.length === 0) {
        setWizardOpen(true);
      }
    } catch (cause) {
      setAdminCheck("invalid");
      setFeedback({
        tone: "error",
        message: errorMessage(cause, { credential: "admin" })
      });
    }
  };

  const forgetAdminKey = () => {
    clearModelAdminKey();
    restoredAdminKeyRef.current = "";
    setAdminKey("");
    setAdminCheck("idle");
    setValidatedSignature("");
    setFeedback({ tone: "success", message: "已忘记管理密钥，当前标签页的会话存档已清除。" });
  };

  const refreshAll = async (preserveDrafts = false) => {
    await load(undefined, preserveDrafts);
    if (hasAdminKey) {
      await loadAdminControl(adminKey.trim(), preserveDrafts);
    }
  };

  const [editingDeployment, setEditingDeployment] = useState<ModelGatewayDeploymentInfo | null>(null);

  const saveDeploymentCapabilities = async (
    deployment: ModelGatewayDeploymentInfo,
    capabilities: ModelGatewayCapabilities
  ) => {
    if (!status?.control || !hasAdminKey || busyAction) return;
    setBusyAction(`capabilities:deployments:${deployment.id}`);
    setFeedback(null);
    try {
      await api.updateProviderDeploymentCapabilities(
        deployment.id,
        status.control.revision,
        capabilities,
        adminKey.trim()
      );
      setEditingDeployment(null);
      await refreshAll(false);
      setFeedback({ tone: "success", message: `${deployment.id} 的能力已更新，配置已热加载。` });
    } catch (cause) {
      setFeedback({ tone: "error", message: errorMessage(cause, { credential: "admin" }) });
    } finally {
      setBusyAction("");
    }
  };

  const setObjectEnabled = async (
    collection: "connections" | "deployments",
    id: string,
    enabled: boolean
  ) => {
    if (!status?.control || !hasAdminKey || busyAction) return;
    if (!enabled) {
      const confirmed = await confirm({
        title: `禁用 ${id}？`,
        message: "如果它正在被用途路由引用，相关聊天、记忆或检索任务可能立即不可用。",
        confirmLabel: "确认禁用",
        cancelLabel: "取消",
        tone: "warning"
      });
      if (!confirmed) return;
    }
    setBusyAction(`object:${collection}:${id}`);
    setFeedback(null);
    try {
      await api.setProviderObjectEnabled(
        collection,
        id,
        status.control.revision,
        enabled,
        adminKey.trim()
      );
      await refreshAll(false);
      setFeedback({
        tone: "success",
        message: `${id} 已${enabled ? "启用" : "禁用"}，配置已热加载。`
      });
    } catch (cause) {
      setFeedback({ tone: "error", message: errorMessage(cause, { credential: "admin" }) });
    } finally {
      setBusyAction("");
    }
  };

  const deleteObject = async (
    collection: "connections" | "deployments" | "pricing",
    id: string
  ) => {
    if (!status?.control || !hasAdminKey || busyAction) return;
    const confirmed = await confirm({
      title: `删除未引用对象 ${id}？`,
      message: "删除后无法从控制台恢复。服务端会再次检查引用关系和 revision；仍被引用时不会删除。",
      confirmLabel: "删除对象",
      cancelLabel: "取消",
      tone: "danger"
    });
    if (!confirmed) return;
    setBusyAction(`delete:${collection}:${id}`);
    setFeedback(null);
    try {
      await api.deleteProviderObject(
        collection,
        id,
        status.control.revision,
        adminKey.trim()
      );
      await refreshAll(false);
      setFeedback({ tone: "success", message: `${id} 已删除。` });
    } catch (cause) {
      setFeedback({ tone: "error", message: errorMessage(cause, { credential: "admin" }) });
    } finally {
      setBusyAction("");
    }
  };

  const setupMode = initialSetup && !isProviderSetupReady(status?.setup);
  const repairMode = Boolean(
    status &&
      (status.setup.state === "configuration_error" || !status.setup.service_ready)
  );

  return (
    <div className="page-stack providers-page">
      <PageHeader
        eyebrow={setupMode ? (repairMode ? "运行配置需处理" : "首次设置 · 第 2 步") : undefined}
        title={setupMode ? (repairMode ? "修复模型与向量配置" : "连接一个模型渠道") : "模型与路由"}
        showTitle={setupMode}
        subtitle={
          setupMode
            ? repairMode
              ? "聊天路由可能仍可用，但整体配置尚未就绪；请按状态提示修复后再继续。"
              : "选择渠道、粘贴供应商 API Key，再选择一个聊天模型即可。"
            : undefined
        }
        action={
          <div className="provider-page-actions">
            {dirty && (
              <button type="button" className="secondary-button" onClick={resetDrafts}>
                放弃草稿
              </button>
            )}
            {dirty && (
              <button
                type="button"
                className="secondary-button"
                onClick={() => {
                  void refreshAll(false).catch((cause) => {
                    setFeedback({ tone: "error", message: errorMessage(cause) });
                  });
                }}
                disabled={loading || Boolean(busyAction)}
              >
                <RefreshCcw size={16} aria-hidden />
                放弃并刷新
              </button>
            )}
            {dirty && status?.control && (
              <>
                <button
                  type="button"
                  className="secondary-button"
                  onClick={() => void validateRoutes()}
                  disabled={!hasAdminKey || Boolean(busyAction)}
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
          {setupMode && !repairMode && <FirstRunProgress status={status} adminCheck={adminCheck} />}
          {status.config_error && <ErrorBlock message={`配置不可用：${status.config_error}`} />}
          {(!setupMode || repairMode) && (
            <RuntimeBanner
              status={status}
              dirty={dirty}
              validated={validated}
              compact={!expertMode}
              onLiveProbe={() => void runLiveProbe()}
              liveProbeBusy={busyAction === "live-probe"}
            />
          )}

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
                state={adminCheck}
                onChange={(value) => {
                  setAdminKey(value);
                  setAdminCheck("idle");
                  setValidatedSignature("");
                }}
                onToggleShow={() => setShowAdminKey((current) => !current)}
                onCheck={() => void checkAdminKey()}
                onForget={forgetAdminKey}
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
                onCreateChannel={() => {
                  setAddModelConnectionId("");
                  setWizardOpen(true);
                }}
                onAddModel={(connection) => {
                  setWizardOpen(false);
                  setAddModelConnectionId(connection.id);
                }}
                onEditDeployment={hasAdminKey ? (deployment) => setEditingDeployment(deployment) : undefined}
              />
              {editingDeployment && (
                <DeploymentCapabilitiesDialog
                  api={api}
                  adminKey={adminKey.trim()}
                  revision={status.control.revision}
                  deployment={editingDeployment}
                  busy={busyAction === `capabilities:deployments:${editingDeployment.id}`}
                  onClose={() => setEditingDeployment(null)}
                  onSave={(capabilities) => void saveDeploymentCapabilities(editingDeployment, capabilities)}
                />
              )}
              {wizardOpen && (
                <NewChannelWizard
                  api={api}
                  adminKey={adminKey}
                  control={status.control}
                  confirm={confirm}
                  onClose={() => setWizardOpen(false)}
                onCompleted={async () => {
                  await refreshAll(false);
                  await onSetupChanged?.();
                }}
              />
              )}
              {addModelConnection && (
                <AddChannelModelPanel
                  api={api}
                  adminKey={adminKey}
                  control={status.control}
                  connection={addModelConnection}
                  onClose={() => setAddModelConnectionId("")}
                  onCompleted={async () => {
                    await refreshAll(false);
                    await onSetupChanged?.();
                  }}
                />
              )}
              {expertMode ? (
                <>
                  <details className="panel provider-advanced-panel">
                    <summary>高级设置：用途与故障切换顺序</summary>
                    <p className="muted">
                      添加备用模型、调整优先顺序或修复已有配置时再展开。
                    </p>
                    <RoutesEditor
                      control={status.control}
                      drafts={drafts}
                      onMove={moveTarget}
                      onToggle={toggleRoute}
                    />
                  </details>
                  {hasAdminKey && (
                    <ObjectManager
                      control={status.control}
                      busyAction={busyAction}
                      onSetEnabled={(collection, id, enabled) => void setObjectEnabled(collection, id, enabled)}
                      onDelete={(collection, id) => void deleteObject(collection, id)}
                    />
                  )}
                </>
              ) : null}
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
  state,
  onChange,
  onToggleShow,
  onCheck,
  onForget
}: {
  value: string;
  show: boolean;
  state: "idle" | "checking" | "valid" | "invalid";
  onChange: (value: string) => void;
  onToggleShow: () => void;
  onCheck: () => void;
  onForget: () => void;
}) {
  if (state === "valid") {
    return (
      <section className="panel provider-admin-access provider-admin-access-compact" aria-labelledby="provider-admin-title">
        <div className="provider-admin-copy">
          <span className="provider-admin-icon" aria-hidden>
            <LockKeyhole size={18} />
          </span>
          <div>
            <h2 id="provider-admin-title">管理密钥已验证</h2>
            <p>只在当前标签页有效，关闭标签页后需要重新输入。</p>
          </div>
        </div>
        <div className="provider-admin-actions">
          <button type="button" className="ghost-button compact" onClick={onForget}>
            <XCircle size={15} aria-hidden />
            忘记管理密钥
          </button>
        </div>
      </section>
    );
  }
  return (
    <section className="panel provider-admin-access" aria-labelledby="provider-admin-title">
      <div className="provider-admin-copy">
        <span className="provider-admin-icon" aria-hidden>
          <LockKeyhole size={18} />
        </span>
        <div>
          <h2 id="provider-admin-title">输入管理密钥</h2>
          <p>新建渠道、添加模型或修改能力都需要管理密钥。它只留在当前标签页，不会写入本机存储。</p>
        </div>
      </div>
      <label className="field-block provider-admin-field">
        <span>管理密钥</span>
        <div className="secret-field">
          <input
            type={show ? "text" : "password"}
            value={value}
            onChange={(event) => onChange(event.target.value)}
            autoComplete="off"
            spellCheck={false}
            placeholder="粘贴 admin.txt 的整行内容；只在验证和保存时发送"
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
      <div className="provider-admin-actions">
        <button
          type="button"
          className="secondary-button"
          onClick={onCheck}
          disabled={!value.trim() || state === "checking"}
        >
          <KeyRound size={16} aria-hidden />
          {state === "checking" ? "正在验证" : "验证管理密钥"}
        </button>
        {state === "invalid" && <span className="field-error">管理密钥无效，请核对后重新输入。</span>}
        {value.trim() && (
          <button type="button" className="ghost-button compact" onClick={onForget}>
            <XCircle size={15} aria-hidden />
            忘记管理密钥
          </button>
        )}
      </div>
      <details className="provider-bootstrap-help">
        <summary>找不到管理密钥？</summary>
        <p>
          安卓 App：从 App 点「打开控制台」会自动带上，无需手动输入；也可以在状态页「高级」里点「复制管理密钥」。
        </p>
        <p>
          Docker 或源码安装：它在安装目录的 <code>credentials/admin.txt</code> 私有文件里（旧版为 <code>admin.key</code>）；不会写入容器日志或环境变量。
        </p>
        <p>丢失后只能换一枚新的；旧密钥会立即失效：</p>
        <p>Docker：在安装目录（默认 memory-platform）打开终端后运行：</p>
        <code>docker compose -f docker-compose.user.yml exec model-gateway modelgw secret set memory-console-admin --stdin</code>
        <p>源码安装：</p>
        <code>services/memory-gateway/.venv/bin/modelgw secret set memory-console-admin --stdin</code>
      </details>
    </section>
  );
}

function FirstRunProgress({
  status,
  adminCheck
}: {
  status: ProvidersStatus;
  adminCheck: "idle" | "checking" | "valid" | "invalid";
}) {
  const hasChannel = status.providers.some((provider) => provider.configured);
  return (
    <section className="panel first-run-progress" aria-label="首次设置进度">
      <div className="panel-header">
        <div>
          <span className="panel-kicker">还需约 2 分钟</span>
          <h2>完成下面三步即可开始聊天</h2>
        </div>
      </div>
      <ol>
        <li className="is-done"><CheckCircle2 size={17} aria-hidden /><span>本地服务已连接</span></li>
        <li className={adminCheck === "valid" ? "is-done" : "is-current"}>
          {adminCheck === "valid" ? <CheckCircle2 size={17} aria-hidden /> : <span>2</span>}
          <span>输入管理密钥（admin.txt）</span>
        </li>
        <li className={status.setup.chat_ready ? "is-done" : adminCheck === "valid" ? "is-current" : ""}>
          {status.setup.chat_ready ? <CheckCircle2 size={17} aria-hidden /> : <span>3</span>}
          <span>{hasChannel ? "选择聊天模型并启用" : "选择渠道并粘贴供应商 API Key"}</span>
        </li>
      </ol>
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
  onCreateChannel,
  onAddModel,
  onEditDeployment
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
  onAddModel: (connection: ModelGatewayConnectionInfo) => void;
  onEditDeployment?: (deployment: ModelGatewayDeploymentInfo) => void;
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
          <p>添加模型或检查连接。密钥替换放在各渠道展开项里。</p>
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
            还没有任何模型渠道。新建第一个渠道后，选择一个聊天模型即可开始用。
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
                  <h3 title={connection.id}>{channelOperatorLabel(connection.channel_operator)}</h3>
                  <StatusPill
                    ok={connection.configured}
                    okText="密钥已配置"
                    badText="缺少密钥"
                  />
                </div>
                <p>{connection.base_url}</p>
                <div className="provider-deployment-chips" aria-label="关联部署">
                  {(deploymentsByConnection[connection.id] || []).map((deployment) =>
                    // Capability flags only steer chat routing; an embedding model
                    // has nothing to edit here, so it stays a plain label.
                    onEditDeployment && deployment.kind === "chat" ? (
                      <button
                        type="button"
                        key={deployment.id}
                        className="provider-deployment-chip-button"
                        onClick={() => onEditDeployment(deployment)}
                        title={`${deployment.id} · 修改这个模型的能力（工具调用、推理等）`}
                        aria-label={`修改模型 ${deployment.id} 的能力`}
                      >
                        <Pencil size={12} aria-hidden />
                        {deployment.upstream_model}
                        {deployment.kind === "chat" && !deployment.capabilities?.tools && (
                          <small>无工具调用</small>
                        )}
                      </button>
                    ) : (
                      <span key={deployment.id} title={deployment.id}>
                        {deployment.upstream_model}
                        {deployment.kind === "embedding" && <small> · 向量</small>}
                      </span>
                    )
                  )}
                </div>
              </div>
              <div className="provider-secret-actions">
                <details className="provider-secret-replace">
                  <summary>替换渠道密钥</summary>
                <label className="field-block">
                  <span className="sr-only">新的渠道密钥</span>
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
                  <button
                    type="button"
                    className="secondary-button"
                    onClick={() => onSaveSecret(connection)}
                    disabled={!adminReady || !value || Boolean(busyAction)}
                  >
                    <KeyRound size={16} aria-hidden />
                    {busyAction === `secret:${connection.id}` ? "正在保存" : "替换密钥"}
                  </button>
                </details>
                <div className="provider-connection-buttons">
                  <button
                    type="button"
                    className="secondary-button"
                    onClick={() => onCheck(connection)}
                    disabled={!adminReady || check === "checking" || Boolean(busyAction)}
                  >
                    <PlugZap size={16} aria-hidden />
                    {check === "checking" ? "正在检查" : "检查连接"}
                  </button>
                  <button
                    type="button"
                    className="secondary-button"
                    onClick={() => onAddModel(connection)}
                    disabled={!adminReady || !connection.configured || Boolean(busyAction)}
                  >
                    <Plus size={16} aria-hidden />
                    添加模型
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

function DeploymentCapabilitiesDialog({
  api,
  adminKey,
  revision,
  deployment,
  busy,
  onClose,
  onSave
}: {
  api: MemoryApi;
  adminKey: string;
  revision: string;
  deployment: ModelGatewayDeploymentInfo;
  busy: boolean;
  onClose: () => void;
  onSave: (capabilities: ModelGatewayCapabilities) => void;
}) {
  const [capabilities, setCapabilities] = useState<ModelGatewayCapabilities>({
    ...deployment.capabilities
  });
  const [probing, setProbing] = useState(false);
  const [probeNote, setProbeNote] = useState<ProviderFeedback | null>(null);
  const detect = async () => {
    if (probing || busy) return;
    setProbing(true);
    setProbeNote(null);
    try {
      const result = await api.probeProviderChannelCapabilities(
        {
          revision,
          connection_id: deployment.connection,
          upstream_model: deployment.upstream_model,
          probes: ["chat", "streaming", "tools", "reasoning", "json_object"]
        },
        adminKey
      );
      const chatOk = result.details?.chat?.ok;
      if (!chatOk) {
        setProbeNote({ tone: "error", message: `基础聊天探测失败：${result.details?.chat?.detail || "未知错误"}。勾选未改动。` });
        return;
      }
      setCapabilities((current) => ({
        ...current,
        tools: Boolean(result.capabilities.tools),
        parallel_tools: Boolean(result.capabilities.parallel_tools),
        reasoning: Boolean(result.capabilities.reasoning),
        json_object: Boolean(result.capabilities.json_object),
        json_schema: Boolean(result.capabilities.json_schema)
      }));
      setProbeNote({ tone: "success", message: "已按实际探测结果勾选（消耗少量额度）。多模态输入无法自动探测，需要时手动勾选。确认后点保存。" });
    } catch (cause) {
      setProbeNote({ tone: "error", message: errorMessage(cause, { credential: "admin" }) });
    } finally {
      setProbing(false);
    }
  };
  return (
    <Modal title={`模型能力：${deployment.upstream_model}`} onClose={onClose} closeDisabled={busy}>
      <p className="muted">
        能力决定路由会不会把某类请求派给这个模型，例如带搜索或 MCP 工具的对话只会派给勾了「工具调用」的模型。不确定就点自动检测。
      </p>
      <div className="provider-wizard-actions provider-detect-row">
        <button type="button" className="secondary-button" onClick={() => void detect()} disabled={probing || busy}>
          <Sparkles size={15} aria-hidden />
          {probing ? "正在探测…" : "自动检测能力"}
        </button>
      </div>
      {probeNote && (
        <div className={`provider-feedback ${probeNote.tone}`} role="status">
          <span>{probeNote.message}</span>
        </div>
      )}
      <fieldset className="provider-capability-field" disabled={busy || probing}>
        <legend>支持的能力</legend>
        <div className="provider-capability-grid">
          {CAPABILITY_OPTIONS.map((option) => (
            <label key={option.key}>
              <input
                type="checkbox"
                checked={Boolean(capabilities[option.key])}
                onChange={(event) =>
                  setCapabilities((current) => ({ ...current, [option.key]: event.target.checked }))
                }
              />
              <span>{option.label}</span>
            </label>
          ))}
        </div>
      </fieldset>
      <div className="provider-wizard-actions">
        <button type="button" className="secondary-button" onClick={onClose} disabled={busy}>
          取消
        </button>
        <button
          type="button"
          className="primary-button"
          onClick={() => onSave(capabilities)}
          disabled={busy}
        >
          {busy ? "保存中…" : "保存能力"}
        </button>
      </div>
    </Modal>
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

function ObjectManager({
  control,
  busyAction,
  onSetEnabled,
  onDelete
}: {
  control: ModelGatewayControlSnapshot;
  busyAction: string;
  onSetEnabled: (
    collection: "connections" | "deployments",
    id: string,
    enabled: boolean
  ) => void;
  onDelete: (
    collection: "connections" | "deployments" | "pricing",
    id: string
  ) => void;
}) {
  const connectionReferences = useMemo(() => {
    const references = new Map<string, string[]>();
    for (const deployment of control.deployments) {
      const list = references.get(deployment.connection) || [];
      list.push(`deployment:${deployment.id}`);
      references.set(deployment.connection, list);
    }
    return references;
  }, [control.deployments]);
  const deploymentReferences = useMemo(() => {
    const references = new Map<string, string[]>();
    for (const route of control.routes) {
      for (const target of route.targets) {
        const list = references.get(target) || [];
        list.push(`route:${route.id}`);
        references.set(target, list);
      }
    }
    return references;
  }, [control.routes]);
  const pricingReferences = useMemo(() => {
    const references = new Map<string, string[]>();
    for (const deployment of control.deployments) {
      if (!deployment.pricing) continue;
      const list = references.get(deployment.pricing) || [];
      list.push(`deployment:${deployment.id}`);
      references.set(deployment.pricing, list);
    }
    return references;
  }, [control.deployments]);
  const pricing = control.pricing || [];

  return (
    <details className="panel provider-advanced-panel provider-object-manager">
      <summary>专家模式：底层对象管理</summary>
      <p className="muted">
        这里展示管理视图中的全部对象，包括未被任何路由引用的候选项。只能删除未引用对象；服务端会在删除前再次核验版本。
      </p>
      <div className="provider-object-groups">
        <ObjectGroup title="Connections" count={control.connections.length}>
          {control.connections.map((connection) => (
            <ObjectRow
              key={connection.id}
              id={connection.id}
              detail={`${connection.channel_operator} · ${connection.usage_scope}`}
              enabled={connection.enabled}
              references={connectionReferences.get(connection.id) || []}
              busy={busyAction.endsWith(`connections:${connection.id}`)}
              onSetEnabled={(enabled) => onSetEnabled("connections", connection.id, enabled)}
              onDelete={() => onDelete("connections", connection.id)}
            />
          ))}
        </ObjectGroup>
        <ObjectGroup title="Deployments" count={control.deployments.length}>
          {control.deployments.map((deployment) => (
            <ObjectRow
              key={deployment.id}
              id={deployment.id}
              detail={`${deployment.kind} · ${deployment.upstream_model} · author ${deployment.model_author || "unknown"}`}
              enabled={deployment.enabled}
              references={deploymentReferences.get(deployment.id) || []}
              busy={busyAction.endsWith(`deployments:${deployment.id}`)}
              onSetEnabled={(enabled) => onSetEnabled("deployments", deployment.id, enabled)}
              onDelete={() => onDelete("deployments", deployment.id)}
            />
          ))}
        </ObjectGroup>
        <ObjectGroup title="Pricing" count={pricing.length}>
          {pricing.map((item) => (
            <PricingObjectRow
              key={item.id}
              item={item}
              references={pricingReferences.get(item.id) || []}
              busy={busyAction === `delete:pricing:${item.id}`}
              onDelete={() => onDelete("pricing", item.id)}
            />
          ))}
        </ObjectGroup>
      </div>
    </details>
  );
}

function ObjectGroup({
  title,
  count,
  children
}: {
  title: string;
  count: number;
  children: ReactNode;
}) {
  return (
    <section className="provider-object-group">
      <header><h3>{title}</h3><span>{count}</span></header>
      {count ? <div className="provider-object-list">{children}</div> : <p>暂无对象</p>}
    </section>
  );
}

function ObjectRow({
  id,
  detail,
  enabled,
  references,
  busy,
  onSetEnabled,
  onDelete
}: {
  id: string;
  detail: string;
  enabled: boolean;
  references: string[];
  busy: boolean;
  onSetEnabled: (enabled: boolean) => void;
  onDelete: () => void;
}) {
  const referenced = references.length > 0;
  return (
    <article className="provider-object-row">
      <div>
        <code>{id}</code>
        <p>{detail}</p>
        <small>{referenced ? `引用：${references.join("、")}` : "未引用，可安全删除"}</small>
      </div>
      <div className="provider-object-actions">
        <button type="button" className="secondary-button" onClick={() => onSetEnabled(!enabled)} disabled={busy}>
          <Power size={15} aria-hidden />{enabled ? "禁用" : "启用"}
        </button>
        <button type="button" className="danger-button" onClick={onDelete} disabled={referenced || busy} title={referenced ? "仍被引用，不能删除" : "删除未引用对象"}>
          <Trash2 size={15} aria-hidden />删除
        </button>
      </div>
    </article>
  );
}

function PricingObjectRow({
  item,
  references,
  busy,
  onDelete
}: {
  item: ModelGatewayPricingInfo;
  references: string[];
  busy: boolean;
  onDelete: () => void;
}) {
  const referenced = references.length > 0;
  return (
    <article className="provider-object-row">
      <div>
        <code>{item.id}</code>
        <p>{item.mode} · {item.currency} / {item.unit_tokens.toLocaleString()} tokens</p>
        <small>{referenced ? `引用：${references.join("、")}` : "未引用，可安全删除"}</small>
      </div>
      <div className="provider-object-actions">
        <button type="button" className="danger-button" onClick={onDelete} disabled={referenced || busy} title={referenced ? "仍被引用，不能删除" : "删除未引用价格记录"}>
          <Trash2 size={15} aria-hidden />删除
        </button>
      </div>
    </article>
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
          <p>上方模型不可用时，Model Gateway 按顺序尝试下一个部署（deployment）。</p>
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
  validated,
  compact = false,
  onLiveProbe,
  liveProbeBusy
}: {
  status: ProvidersStatus;
  dirty: boolean;
  validated: boolean;
  compact?: boolean;
  onLiveProbe?: () => void;
  liveProbeBusy?: boolean;
}) {
  const { runtime, embedding, setup } = status;
  const probe = setup.live_probe;
  const embeddingMode = embedding.mode === "pinned" ? "固定契约" : "自动契约";
  const embeddingState = {
    ready: "可用",
    off: "已关闭（关键词检索）",
    invalid: "契约无效",
    unavailable: "暂不可用"
  }[embedding.state];
  if (compact) {
    return (
      <div className="runtime-banner runtime-banner-compact" role="status">
        <span>
          向量{embeddingState}
          {embedding.configured ? ` · ${embedding.dimensions} 维` : ""}
          {validated ? " · 草稿已校验" : dirty ? " · 有未应用草稿" : ""}
        </span>
        {onLiveProbe && setup.chat_ready && (
          <button
            type="button"
            className="ghost-button compact"
            onClick={onLiveProbe}
            disabled={liveProbeBusy}
          >
            {liveProbeBusy ? "探测中…" : "探测上游"}
          </button>
        )}
      </div>
    );
  }
  return (
    <details className="panel provider-advanced-panel runtime-banner-details">
      <summary>
        运行信息 · 向量{embeddingState}
        {validated ? " · 草稿已校验" : dirty ? " · 有未应用草稿" : ""}
      </summary>
    <div className="runtime-banner">
      <div>
        <strong>配置来源</strong>
        <span>{runtime.model_gateway_enabled ? "独立 Model Gateway" : "本项目兼容配置"}</span>
      </div>
      <div>
        <strong>客户端接入</strong>
        <span>{window.location.origin}/v1 · 模型 {CLIENT_MODEL_ID}</span>
      </div>
      <div>
        <strong>向量模型</strong>
        <span>
          {embedding.model} · {embeddingMode} · {embeddingState}
          {embedding.configured ? ` · ${embedding.dimensions} 维` : ""}
          {embedding.code ? ` · ${embedding.code}` : ""}
        </span>
      </div>
      <div>
        <strong>页面状态</strong>
        <span>{validated ? "草稿已校验" : dirty ? "有未应用草稿" : "与运行配置一致"}</span>
      </div>
      <div>
        <strong>上游探测</strong>
        <span>
          {probe
            ? probe.ok
              ? `可达 · ${probe.latency_ms ?? "?"} ms${probe.cached ? "（缓存）" : ""}`
              : `失败 · ${probe.message || probe.code}`
            : setup.chat_ready
              ? "尚未探测"
              : "配置未就绪"}
        </span>
        {onLiveProbe && setup.chat_ready && (
          <button
            type="button"
            className="ghost-button compact"
            onClick={onLiveProbe}
            disabled={liveProbeBusy}
            style={{ marginTop: 6 }}
          >
            {liveProbeBusy ? "探测中…" : "立即探测上游"}
          </button>
        )}
      </div>
    </div>
    </details>
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
