/* Reasoning disclosure adapted from DeepSeek Harness ReasoningRow (MIT). */
import { Brain, ChevronRight } from "lucide-react";
import { useState } from "react";
import styles from "./ReasoningRow.module.css";

export function ReasoningRow({ text, running }: { text: string; running: boolean }) {
  const [expanded, setExpanded] = useState(false);
  const visible = text.trimEnd();
  const boundary = running ? visible.lastIndexOf("\n") : visible.indexOf("\n");
  const summary = boundary < 0
    ? visible
    : running ? visible.slice(boundary + 1) : visible.slice(0, boundary);
  return (
    <div className={styles.block} data-state={running ? "running" : "ok"}>
      <button type="button" className={styles.summary} aria-expanded={expanded} onClick={() => setExpanded((value) => !value)}>
        <Brain size={14} />
        <span className={styles.title}>Think</span>
        <i className={styles.separator} aria-hidden />
        <span className={styles.preview} data-follow-end={running || undefined}>{summary}</span>
        <ChevronRight size={13} className={expanded ? styles.open : undefined} />
      </button>
      {expanded && <div className={`${styles.content} reasoning-content`}>{text}</div>}
    </div>
  );
}
