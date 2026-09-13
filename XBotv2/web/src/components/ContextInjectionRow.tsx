import { Activity, ChevronRight, Layers3 } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { RuntimeEntry } from "../state/runtime";
import styles from "./ContextInjectionRow.module.css";

export function ContextInjectionRow({ entry }: { entry: RuntimeEntry }) {
  if (entry.source === "turn") {
    return (
      <div className={`${styles.row} ${styles.lifecycle}`} role="status">
        <Activity size={14} />
        <span>{entry.content}</span>
        <code>{entry.event}</code>
      </div>
    );
  }
  const compact = entry.source === "compact";
  return (
    <details className={`${styles.row}${compact ? ` ${styles.compact}` : ""}`}>
      <summary>
        <Layers3 size={14} />
        <span>{compact ? "Compacted context" : "Injected context"}</span>
        <code>{entry.source}{entry.event ? ` · ${entry.event}` : ""}</code>
        <ChevronRight size={13} className={styles.chevron} />
      </summary>
      <div className={`${styles.body} markdown-body`}>
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{entry.content}</ReactMarkdown>
      </div>
    </details>
  );
}
