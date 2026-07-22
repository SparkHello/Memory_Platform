import { RefreshCcw, ShieldAlert } from "lucide-react";

export function LoadingBlock({ label }: { label: string }) {
  return (
    <div className="state-block" role="status" aria-live="polite">
      <RefreshCcw size={18} className="spin" />
      {label}
    </div>
  );
}

export function ErrorBlock({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="state-block error" role="alert">
      <ShieldAlert size={18} />
      <span>{message}</span>
      {onRetry && (
        <button className="secondary-button compact" type="button" onClick={onRetry}>
          重试
        </button>
      )}
    </div>
  );
}

export function EmptyBlock({
  label,
  hint,
  compact = false,
  action
}: {
  label: string;
  hint?: string;
  compact?: boolean;
  action?: { label: string; onClick: () => void };
}) {
  if (compact) {
    return <div className="state-block empty compact">{label}</div>;
  }
  return (
    <div className="state-block empty">
      <svg
        className="empty-constellation"
        width="120"
        height="40"
        viewBox="0 0 120 40"
        aria-hidden="true"
      >
        <line x1="18" y1="28" x2="52" y2="11" stroke="currentColor" strokeDasharray="3 4" />
        <line x1="52" y1="11" x2="88" y2="24" stroke="currentColor" strokeDasharray="3 4" />
        <line x1="88" y1="24" x2="106" y2="9" stroke="currentColor" strokeDasharray="3 4" opacity="0.6" />
        <circle cx="18" cy="28" r="4" fill="var(--type-episodic)" />
        <circle cx="52" cy="11" r="5" fill="var(--type-reflective)" />
        <circle cx="88" cy="24" r="4" fill="var(--type-emotional)" />
        <circle cx="106" cy="9" r="3" fill="var(--type-procedural)" />
      </svg>
      <b>{label}</b>
      {hint && <p>{hint}</p>}
      {action && (
        <button className="secondary-button compact empty-action" type="button" onClick={action.onClick}>
          {action.label}
        </button>
      )}
    </div>
  );
}
