import { ChevronRight, Layers3 } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { RuntimeEntry } from "../state/runtime";
import styles from "./ContextInjectionRow.module.css";

export function ContextInjectionRow({ entry }: { entry: RuntimeEntry }) {
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
