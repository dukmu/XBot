import { Activity, ChevronRight, Layers3 } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { RuntimeEntry } from "../state/runtime";
import styles from "./ContextInjectionRow.module.css";

export function ContextInjectionRow({ entry }: { entry: RuntimeEntry }) {
  const lifecycle = entry.source === "turn" || entry.source === "compact";
  if (lifecycle) {
    const Icon = entry.source === "compact" ? Layers3 : Activity;
    return (
      <div className={`${styles.row} ${styles.lifecycle}`} role="status">
        <Icon size={14} />
        <span>{entry.content}</span>
        <code>{entry.event}</code>
      </div>
    );
  }
  return (
    <details className={styles.row}>
      <summary>
        <Layers3 size={14} />
        <span>Injected context</span>
        <code>{entry.source} · {entry.event}</code>
        <ChevronRight size={13} className={styles.chevron} />
      </summary>
      <div className={`${styles.body} markdown-body`}>
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{entry.content}</ReactMarkdown>
      </div>
    </details>
  );
}
