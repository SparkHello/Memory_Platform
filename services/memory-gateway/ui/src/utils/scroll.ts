export function scrollWorkspaceToTop(): void {
  const content = document.querySelector<HTMLElement>(".content-area");
  content?.scrollTo?.({ top: 0, left: 0, behavior: "auto" });

  // 桌面端由 content-area 滚动；移动端当前使用页面滚动。两者都复位，
  // 可避免在响应式断点切换后继承上一页的位置。
  window.scrollTo?.({ top: 0, left: 0, behavior: "auto" });
}
