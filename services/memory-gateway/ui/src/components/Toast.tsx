import { X } from "lucide-react";
import type { ToastMessage } from "../hooks/useToast";

export function ToastView({ toast, onDismiss }: { toast: ToastMessage; onDismiss: () => void }) {
  return (
    <div className={`toast ${toast.kind}`} role={toast.kind === "error" ? "alert" : "status"} aria-live="polite">
      <span className="toast-text">{toast.message}</span>
      <button className="toast-close" type="button" onClick={onDismiss} aria-label="关闭提示">
        <X size={14} />
      </button>
    </div>
  );
}
