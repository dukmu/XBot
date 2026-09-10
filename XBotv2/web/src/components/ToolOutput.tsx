/* Head/tail output treatment adapted from DeepSeek Harness TerminalBlock (MIT). */
import { useMemo, useState } from "react";
import styles from "./ToolOutput.module.css";

const MAX_LINES = 16;
const MAX_COLLAPSED_CHARS = 20_000;

export function ToolOutput({ value, label }: { value: unknown; label?: string }) {
  const text = useMemo(() => formatValue(value), [value]);
  const preview = useMemo(() => collapsedOutput(text), [text]);
  const [expanded, setExpanded] = useState(false);
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
      <pre>{expanded ? text : preview.text}</pre>
      {preview.capped && (
        <button
          type="button"
          className={styles.expand}
          aria-expanded={expanded}
          onClick={() => setExpanded((value) => !value)}
        >
          {expanded ? "Collapse output" : preview.label}
        </button>
      )}
    </div>
  );
}

function collapsedOutput(text: string): { text: string; capped: boolean; label: string } {
  const lines = text.split("\n");
  if (lines.length > 1 && lines.at(-1) === "") lines.pop();
  if (lines.length > MAX_LINES) {
    const head = Math.ceil(MAX_LINES / 2);
    const tail = Math.floor(MAX_LINES / 2);
    const hidden = lines.length - MAX_LINES;
    const selected = [
      ...lines.slice(0, head),
      `… ${hidden} lines hidden …`,
      ...lines.slice(-tail),
    ].join("\n");
    return {
      text: capCharacters(selected),
      capped: true,
      label: `Show ${hidden} hidden lines`,
    };
  }
  if (text.length > MAX_COLLAPSED_CHARS) {
    return {
      text: capCharacters(text),
      capped: true,
      label: "Show full output",
    };
  }
  return { text, capped: false, label: "" };
}

function capCharacters(text: string): string {
  if (text.length <= MAX_COLLAPSED_CHARS) return text;
  const half = MAX_COLLAPSED_CHARS / 2;
  return `${text.slice(0, half)}\n… output truncated …\n${text.slice(-half)}`;
}

function formatValue(value: unknown): string {
  if (typeof value === "string") return value;
  try { return JSON.stringify(value, null, 2); } catch { return String(value); }
}
