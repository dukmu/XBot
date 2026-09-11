/* Head/tail output treatment adapted from DeepSeek Harness TerminalBlock (MIT). */
import { useMemo, useState } from "react";
import styles from "./ToolOutput.module.css";

export function ToolOutput({ value, label }: { value: unknown; label?: string }) {
  const text = useMemo(() => formatValue(value), [value]);
  const [copied, setCopied] = useState(false);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1200);
    } catch {
      setCopied(false);
    }
  };

  return (
    <div className={styles.output}>
      {(label || text) && (
        <div className={styles.header}>
          {label && <span>{label}</span>}
          {text && <button type="button" onClick={() => void copy()}>{copied ? "Copied" : "Copy"}</button>}
        </div>
      )}
      <pre>{text}</pre>
    </div>
  );
}

function formatValue(value: unknown): string {
  if (typeof value === "string") return value;
  try { return JSON.stringify(value, null, 2); } catch { return String(value); }
}
