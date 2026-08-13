import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => ({
  base: "/ui/",
  plugins: [react()],
  server: {
    port: Number(process.env.PORT) || 5173,
    // E2E uses a non-trustworthy LAN-style origin mapped to 127.0.0.1 so the
    // clipboard fallback is exercised without opening a real network service.
    allowedHosts: mode === "e2e" ? ["memory-platform.test"] : [],
    // 覆盖 Console 全部顶级 API 前缀，dev server 才能完整代理到本机网关
    proxy: Object.fromEntries(
      ["/health", "/memories", "/auth", "/usage", "/providers", "/knowledge"].map((path) => [
        path,
        { target: "http://localhost:2026", changeOrigin: true }
      ])
    )
  },
  build: {
    outDir: "dist",
    emptyOutDir: true
  }
}));
