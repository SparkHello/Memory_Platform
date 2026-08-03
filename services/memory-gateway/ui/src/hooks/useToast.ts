import { useCallback, useEffect, useState } from "react";

export type ToastKind = "success" | "error" | "info";

export type ToastMessage = {
  kind: ToastKind;
  message: string;
};

export function useToast() {
  const [toast, setToast] = useState<ToastMessage | null>(null);

  const notify = useCallback((message: string, kind: ToastKind = "info") => {
    setToast({ message, kind });
  }, []);

  const clearToast = useCallback(() => {
    setToast(null);
  }, []);

  useEffect(() => {
    if (!toast) {
      return;
    }
    // 错误提示停留到用户手动关闭，避免还没读完就消失
    if (toast.kind === "error") {
      return;
    }
    const timer = window.setTimeout(clearToast, 3200);
    return () => window.clearTimeout(timer);
  }, [clearToast, toast]);

  return { toast, notify, clearToast };
}
