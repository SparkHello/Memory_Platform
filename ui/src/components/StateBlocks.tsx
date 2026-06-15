import { RefreshCcw, ShieldAlert } from "lucide-react";

export function LoadingBlock({ label }: { label: string }) {
  return (
    <div className="state-block">
      <RefreshCcw size={18} className="spin" />
      {label}
    </div>
  );
}

export function ErrorBlock({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="state-block error">
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

export function EmptyBlock({ label, compact = false }: { label: string; compact?: boolean }) {
  return <div className={`state-block empty ${compact ? "compact" : ""}`}>{label}</div>;
}
