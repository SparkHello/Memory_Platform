import { useCallback, useState } from "react";
import type { ReactNode } from "react";

export type ConfirmTone = "default" | "danger" | "warning";

export type ConfirmOptions = {
  title?: string;
  message: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  tone?: ConfirmTone;
};

export type ConfirmFn = (options: ConfirmOptions | string) => Promise<boolean>;

type PendingConfirm = Required<Pick<ConfirmOptions, "confirmLabel" | "cancelLabel" | "tone">> &
  Pick<ConfirmOptions, "title" | "message"> & {
    resolve: (confirmed: boolean) => void;
  };

export function useConfirm() {
  const [pending, setPending] = useState<PendingConfirm | null>(null);

  const confirm: ConfirmFn = useCallback((options) => {
    const normalized: ConfirmOptions =
      typeof options === "string" ? { message: options } : options;

    return new Promise<boolean>((resolve) => {
      setPending({
        title: normalized.title,
        message: normalized.message,
        confirmLabel: normalized.confirmLabel || "确认",
        cancelLabel: normalized.cancelLabel || "取消",
        tone: normalized.tone || "default",
        resolve
      });
    });
  }, []);

  const close = useCallback(
    (confirmed: boolean) => {
      setPending((current) => {
        current?.resolve(confirmed);
        return null;
      });
    },
    []
  );

  return {
    confirm,
    confirmState: pending
      ? {
          title: pending.title,
          message: pending.message,
          confirmLabel: pending.confirmLabel,
          cancelLabel: pending.cancelLabel,
          tone: pending.tone
        }
      : null,
    resolveConfirm: close
  };
}
