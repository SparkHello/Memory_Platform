import type { ToastMessage } from "../hooks/useToast";

export function ToastView({ toast }: { toast: ToastMessage }) {
  return <div className={`toast ${toast.kind}`}>{toast.message}</div>;
}
