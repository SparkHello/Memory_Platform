import React from "react";
import ReactDOM from "react-dom/client";
import { App } from "./App";
import "./styles/tokens.css";
import "./styles/ambient.css";
import "./styles/base.css";
import "./styles/components.css";
import "./styles/pages.css";
import "./styles/morning-crystal.css";
import "./styles/accessibility.css";
import "./styles/knowledge.css";

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
