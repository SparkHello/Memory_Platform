export async function copyText(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch {
      // 局域网 HTTP 页面通常不属于 secure context；即使 Clipboard API
      // 存在也可能拒绝调用，此时继续使用兼容复制路径。
    }
  }

  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.style.position = "fixed";
  textarea.style.left = "-9999px";
  textarea.style.opacity = "0";
  textarea.setAttribute("readonly", "");
  document.body.appendChild(textarea);
  try {
    textarea.focus();
    textarea.select();
    if (!document.execCommand?.("copy")) {
      throw new Error("当前浏览器不允许自动复制，请手动选择并复制内容");
    }
  } finally {
    textarea.remove();
  }
}

export function downloadFile(filename: string, content: string, type: string) {
  const blob = new Blob([content], { type });
  downloadBlob(filename, blob);
}

export function downloadBlob(filename: string, blob: Blob) {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
  // 延迟 revoke，避免部分浏览器在 click() 后同步回收导致下载被打断。
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
