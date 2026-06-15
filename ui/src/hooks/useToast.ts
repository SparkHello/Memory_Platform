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
    const timer = window.setTimeout(clearToast, 3200);
    return () => window.clearTimeout(timer);
  }, [clearToast, toast]);

  return { toast, notify, clearToast };
}
