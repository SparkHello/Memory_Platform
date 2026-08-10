import { useId } from "react";
import { X } from "lucide-react";
import type { ConfirmOptions } from "../hooks/useConfirm";
import { useDialogA11y } from "../hooks/useDialogA11y";

export function ConfirmDialog({
  state,
  onResolve
}: {
  state: (ConfirmOptions & { confirmLabel: string; cancelLabel: string }) | null;
  onResolve: (confirmed: boolean) => void;
}) {
  const titleId = useId();
  const dialogRef = useDialogA11y<HTMLDivElement>(() => onResolve(false), Boolean(state));

  if (!state) {
    return null;
  }

  const tone = state.tone || "default";

  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={(event) => {
      if (event.target === event.currentTarget) onResolve(false);
    }}>
      <div ref={dialogRef} className={`modal-card confirm-card confirm-${tone}`} role="dialog" aria-modal="true" aria-labelledby={titleId} tabIndex={-1}>
        <div className="drawer-header">
          <h2 id={titleId}>{state.title || "请确认"}</h2>
          <button className="icon-button" type="button" onClick={() => onResolve(false)} title="关闭" aria-label="关闭">
            <X size={18} />
          </button>
        </div>
        <div className="confirm-body">{state.message}</div>
        <div className="drawer-actions end">
          <button
            className="ghost-button"
            type="button"
            data-autofocus={tone === "danger" || tone === "warning" ? true : undefined}
            onClick={() => onResolve(false)}
          >
            {state.cancelLabel}
          </button>
          <button
            className={tone === "danger" ? "danger-button" : "primary-button"}
            type="button"
            data-autofocus={tone === "default" ? true : undefined}
            onClick={() => onResolve(true)}
          >
            {state.confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
