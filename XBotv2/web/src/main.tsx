import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./app/App";
// Token layer vendored from DeepSeek Harness (see styles/dsh/README.md); it
// loads first so the presentation sheets below build on the same variables.
import "./styles/dsh/base.css";
import "./styles/dsh/design-platform.css";
import "./styles/dsh/scrollbar.css";
import "./styles/dsh/shiki.css";
import "./styles/dsh/gradient-shadow-text.css";
import "./styles/global.css";
import "./styles/dsh.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
