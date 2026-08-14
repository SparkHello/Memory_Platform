import { useEffect, useRef } from "react";
import type { ConfirmFn } from "./useConfirm";

// 未保存修改保护：dirty 时拦截刷新/关闭和站内导航点击，确认后才放行。
// 站内导航由 App 先改 state 再改 hash，hashchange 触发时本页已卸载，
// 只能在捕获阶段拦截导航控件的点击，确认后重新触发原按钮完成跳转。
export function useUnsavedChangesGuard(dirty: boolean, message: string, confirm: ConfirmFn) {
  const allowNextClickRef = useRef(false);
  const dirtyRef = useRef(dirty);
  dirtyRef.current = dirty;

  useEffect(() => {
    if (!dirty) return;
    const onBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    const onClickCapture = (event: MouseEvent) => {
      if (allowNextClickRef.current) {
        allowNextClickRef.current = false;
        return;
      }
      if (!dirtyRef.current) return;
      const target = event.target instanceof Element ? event.target : null;
      const button = target?.closest<HTMLElement>(
        ".sidebar .nav-item, .mobile-bottom-nav button:not(:last-child), .mobile-more-grid button, .avatar-chip"
      );
      if (!button || button.classList.contains("active") || button.getAttribute("aria-current") === "page") {
        return;
      }
      event.preventDefault();
      event.stopPropagation();
      void confirm({
        title: "离开当前页面？",
        message,
        confirmLabel: "放弃修改并离开",
        cancelLabel: "继续编辑",
        tone: "warning"
      }).then((confirmed) => {
        if (confirmed) {
          allowNextClickRef.current = true;
          button.click();
        }
      });
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    document.addEventListener("click", onClickCapture, true);
    return () => {
      window.removeEventListener("beforeunload", onBeforeUnload);
      document.removeEventListener("click", onClickCapture, true);
    };
  }, [dirty, message, confirm]);
}
