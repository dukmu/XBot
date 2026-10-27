import { Activity, ChevronRight, Layers3 } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { RuntimeEntry } from "../state/runtime";
import styles from "./ContextInjectionRow.module.css";

/** Ported copy: the title dsh gives an injected context row. */
export const CONTEXT_INJECTION = "Context injection";
export const COMPACTED_CONTEXT = "Compacted context";

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
  // Ported row shape: the title, then the producer, then the detail — so the
  // accessible name reads `Context injection <source>`, as it does in dsh.
  return (
    <details className={`${styles.row}${compact ? ` ${styles.compact}` : ""}`}>
      <summary>
        <Layers3 size={14} />
        <span>{compact ? COMPACTED_CONTEXT : CONTEXT_INJECTION}</span>
        <i className={styles.separator} aria-hidden />
        <code className={styles.source}>{entry.source}</code>
        {entry.event && <span className={styles.event}>{entry.event}</span>}
        <ChevronRight size={13} className={styles.chevron} />
      </summary>
      <div className={`${styles.body} markdown-body`}>
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{entry.content}</ReactMarkdown>
        {entry.images?.map((media, index) => media.href
          ? <a key={index} href={media.href} target="_blank" rel="noreferrer">{media.src ? <img src={media.src} alt={media.label} /> : media.label}</a>
          : <span key={index}>{media.label}</span>)}
      </div>
    </details>
  );
}
