import { useId, type ReactNode } from "react";
import { X } from "lucide-react";
import { useDialogA11y } from "../hooks/useDialogA11y";

export function Modal({
  title,
  children,
  onClose,
  className = "",
  closeDisabled = false
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
  className?: string;
  closeDisabled?: boolean;
}) {
  const titleId = useId();
  const dialogRef = useDialogA11y<HTMLDivElement>(onClose);
  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={(event) => {
      if (!closeDisabled && event.target === event.currentTarget) onClose();
    }}>
      <div
        ref={dialogRef}
        className={`modal-card ${className}`.trim()}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
      >
        <div className="drawer-header">
          <h2 id={titleId}>{title}</h2>
          <button className="icon-button" type="button" disabled={closeDisabled} onClick={onClose} title="关闭" aria-label="关闭">
            <X size={18} />
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}

export function Drawer({
  title,
  children,
  onClose
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
}) {
  const titleId = useId();
  const dialogRef = useDialogA11y<HTMLElement>(onClose);
  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={(event) => {
      if (event.target === event.currentTarget) onClose();
    }}>
      <aside ref={dialogRef} className="drawer-card" role="dialog" aria-modal="true" aria-labelledby={titleId} tabIndex={-1}>
        <div className="drawer-header">
          <h2 id={titleId}>{title}</h2>
          <button className="icon-button" type="button" onClick={onClose} title="关闭" aria-label="关闭">
            <X size={18} />
          </button>
        </div>
        {children}
      </aside>
    </div>
  );
}
