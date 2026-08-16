import { Component, type ErrorInfo, type ReactNode } from "react";
import { ShieldAlert } from "lucide-react";
import { copyText } from "../utils/files";

type ErrorBoundaryProps = {
  variant: "page" | "overlay";
  /** 任一引用变化即自动清除错误，用于「切换页面即恢复」。 */
  resetKeys?: readonly unknown[];
  /** 仅 page：跳回工作室的入口。 */
  onGoHome?: () => void;
  /** 仅 overlay：关闭该悬浮层的入口。 */
  onDismiss?: () => void;
  /** 仅 page：根级兜底边界用「重新加载」代替「重试/返回工作室」。 */
  onReload?: () => void;
  children: ReactNode;
};

type ErrorBoundaryState = {
  error: Error | null;
  componentStack: string;
  copied: boolean;
};

const MAX_MESSAGE_LENGTH = 200;

function truncateMessage(message: string): string {
  if (message.length <= MAX_MESSAGE_LENGTH) return message;
  return `${message.slice(0, MAX_MESSAGE_LENGTH)}…`;
}

export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null, componentStack: "", copied: false };

  static getDerivedStateFromError(error: Error): Partial<ErrorBoundaryState> {
    return { error, copied: false };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("[console] 渲染异常", error, info.componentStack);
    this.setState({ componentStack: info.componentStack || "" });
  }

  componentDidUpdate(prevProps: ErrorBoundaryProps): void {
    if (!this.state.error || !this.props.resetKeys) return;
    const changed = this.props.resetKeys.some(
      (key, index) => key !== prevProps.resetKeys?.[index]
    );
    if (changed) {
      this.setState({ error: null, componentStack: "", copied: false });
    }
  }

  private clearError = () => {
    this.setState({ error: null, componentStack: "", copied: false });
  };

  private handleRetry = () => this.clearError();

  private handleGoHome = () => {
    // 先清错再导航：父组件 setState 与本次 setState 同批执行，避免旧页面短暂复现。
    this.clearError();
    this.props.onGoHome?.();
  };

  private handleDismiss = () => {
    this.clearError();
    this.props.onDismiss?.();
  };

  private buildDiagnostic(): string {
    const { error, componentStack } = this.state;
    return [
      `错误：${error?.message ?? "未知错误"}`,
      error?.stack ? `\n堆栈：\n${error.stack}` : "",
      componentStack ? `\n组件栈：\n${componentStack}` : "",
      `\n当前地址：${window.location.href}`
    ].join("\n");
  }

  private handleCopy = async () => {
    try {
      await copyText(this.buildDiagnostic());
      this.setState({ copied: true });
    } catch {
      // copyText 仅在其兼容回退也失败时抛错；保持错误页可用，不做额外提示。
    }
  };

  render() {
    const { error, copied } = this.state;
    if (!error) return this.props.children;

    if (this.props.variant === "overlay") {
      return (
        <div className="error-boundary error-boundary-overlay" role="alert">
          <div className="error-boundary-overlay-card">
            <span className="error-boundary-icon">
              <ShieldAlert size={18} />
            </span>
            <div className="error-boundary-overlay-body">
              <strong>此面板出现错误</strong>
              <p>{truncateMessage(error.message || "未知错误")}</p>
            </div>
            <div className="error-boundary-actions">
              <button className="secondary-button" type="button" onClick={this.handleDismiss}>
                关闭
              </button>
              <button
                className="ghost-button compact"
                type="button"
                onClick={() => void this.handleCopy()}
              >
                {copied ? "已复制" : "复制诊断信息"}
              </button>
            </div>
          </div>
        </div>
      );
    }

    return (
      <div className="page-stack error-boundary-page" role="alert">
        <div className="page-header">
          <div>
            <h1>页面出现错误</h1>
            <p>当前页面渲染时发生异常，侧栏导航和其他功能不受影响。</p>
          </div>
        </div>
        <div className="panel error-boundary-card">
          {error.message && (
            <code className="error-boundary-detail">{truncateMessage(error.message)}</code>
          )}
          <div className="error-boundary-actions">
            {this.props.onReload ? (
              <button className="primary-button" type="button" onClick={this.props.onReload}>
                重新加载
              </button>
            ) : (
              <>
                <button className="primary-button" type="button" autoFocus onClick={this.handleRetry}>
                  重试
                </button>
                {this.props.onGoHome && (
                  <button className="secondary-button" type="button" onClick={this.handleGoHome}>
                    返回工作室
                  </button>
                )}
                <button
                  className="ghost-button"
                  type="button"
                  onClick={() => void this.handleCopy()}
                >
                  {copied ? "已复制" : "复制诊断信息"}
                </button>
              </>
            )}
          </div>
        </div>
      </div>
    );
  }
}
