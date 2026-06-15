import { useEffect } from "react";
import { X } from "lucide-react";
import type { ConfirmOptions } from "../hooks/useConfirm";

export function ConfirmDialog({
  state,
  onResolve
}: {
  state: (ConfirmOptions & { confirmLabel: string; cancelLabel: string }) | null;
  onResolve: (confirmed: boolean) => void;
}) {
  useEffect(() => {
    if (!state) {
      return;
    }
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onResolve(false);
      }
    };
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, [onResolve, state]);

  if (!state) {
    return null;
  }

  const tone = state.tone || "default";

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true">
      <div className={`modal-card confirm-card confirm-${tone}`}>
        <div className="drawer-header">
          <h2>{state.title || "请确认"}</h2>
          <button className="icon-button" type="button" onClick={() => onResolve(false)} title="关闭">
            <X size={18} />
          </button>
        </div>
        <div className="confirm-body">{state.message}</div>
        <div className="drawer-actions end">
          <button className="ghost-button" type="button" onClick={() => onResolve(false)}>
            {state.cancelLabel}
          </button>
          <button
            className={tone === "danger" ? "danger-button" : "primary-button"}
            type="button"
            onClick={() => onResolve(true)}
          >
            {state.confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
