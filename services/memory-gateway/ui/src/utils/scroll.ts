export function scrollWorkspaceToTop(): void {
  const content = document.querySelector<HTMLElement>(".content-area");
  content?.scrollTo?.({ top: 0, left: 0, behavior: "auto" });

  // 桌面和手机都由 .content-area 滚动。手机上 html/body/#root 是
  // overflow:hidden，避免地址栏收缩把 fixed 底栏顶走。两者都复位，
  // 可避免在响应式断点切换后继承上一页的位置。
  window.scrollTo?.({ top: 0, left: 0, behavior: "auto" });
}
