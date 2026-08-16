import React from "react";
import ReactDOM from "react-dom/client";
import { App } from "./App";
import { ErrorBoundary } from "./components/ErrorBoundary";
import "./styles/tokens.css";
import "./styles/ambient.css";
import "./styles/base.css";
import "./styles/components.css";
import "./styles/pages.css";
import "./styles/morning-crystal.css";
import "./styles/accessibility.css";
import "./styles/knowledge.css";
import "./styles/conversation.css";
import "./styles/usage.css";
import "./styles/providers.css";

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    {/* 根级兜底边界：App 自身崩溃时的最后防线，仅提供整页重载。 */}
    <ErrorBoundary variant="page" onReload={() => window.location.reload()}>
      <App />
    </ErrorBoundary>
  </React.StrictMode>
);
