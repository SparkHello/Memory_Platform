import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  base: "/ui/",
  plugins: [react()],
  server: {
    proxy: {
      "/health": {
        target: "http://localhost:2026",
        changeOrigin: true
      },
      "/memories": {
        target: "http://localhost:2026",
        changeOrigin: true
      }
    }
  },
  build: {
    outDir: "dist",
    emptyOutDir: true
  }
});
