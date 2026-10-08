import { useCallback, useEffect, useState } from "react";

// warning：操作成功但有需要用户读完的提示（例如导入警告），和 error 一样不自动消失。
export type ToastKind = "success" | "error" | "warning" | "info";

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
    // 错误和警告提示停留到用户手动关闭，避免还没读完就消失
    if (toast.kind === "error" || toast.kind === "warning") {
      return;
    }
    const timer = window.setTimeout(clearToast, 3200);
    return () => window.clearTimeout(timer);
  }, [clearToast, toast]);

  return { toast, notify, clearToast };
}
