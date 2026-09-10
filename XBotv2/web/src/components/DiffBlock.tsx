/* Inline file diff adapted from DeepSeek Harness DiffBlock (MIT). */
import { useEffect, useMemo, useRef, useState } from "react";
import styles from "./DiffBlock.module.css";

export const DEFAULT_DIFF_MAX_LINES = 16;

export interface DiffHunk {
  path: string;
  oldText: string | null;
  newText: string;
}

interface DiffRow {
  kind: "path" | "del" | "add" | "gap";
  text: string;
}

export function DiffBlock({
  diffs,
  maxLines = DEFAULT_DIFF_MAX_LINES,
}: {
  diffs: DiffHunk[];
  maxLines?: number;
}) {
  const model = useMemo(() => buildDiff(diffs), [diffs]);
  const [expanded, setExpanded] = useState(false);
  const [copied, setCopied] = useState(false);
  const copyTimer = useRef<number | null>(null);

  useEffect(() => () => {
    if (copyTimer.current !== null) window.clearTimeout(copyTimer.current);
  }, []);

  if (model.rows.length === 0) return null;
  const hidden = Math.max(0, model.rows.length - maxLines);
  const capped = hidden > 0 && !expanded;
  const headLength = Math.ceil(maxLines / 2);
  const tailLength = maxLines - headLength;
  const head = capped ? model.rows.slice(0, headLength) : model.rows;
  const tail = capped ? model.rows.slice(-tailLength) : [];

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(copyText(model.rows));
      setCopied(true);
      copyTimer.current = window.setTimeout(() => {
        copyTimer.current = null;
        setCopied(false);
      }, 1000);
    } catch {
      setCopied(false);
    }
  };

  return (
    <div className={styles.block} data-diff="">
      <button
        type="button"
        className={styles.copyButton}
        aria-label={copied ? "Diff copied" : "Copy diff"}
        onClick={() => void copy()}
      >
        {copied ? "Copied" : "Copy"}
      </button>
      <div className={styles.body}>
        {head.map((row, index) => <DiffLine key={index} row={row} />)}
        {hidden > 0 && (
          <button
            type="button"
            className={styles.expand}
            aria-expanded={expanded}
            aria-label={expanded ? "Collapse diff" : `Show ${hidden} hidden diff lines`}
            onClick={() => setExpanded((value) => !value)}
          >
            {expanded ? "Collapse" : `… ${hidden} lines hidden …`}
          </button>
        )}
        {tail.map((row, index) => <DiffLine key={index} row={row} />)}
      </div>
      <div className={styles.footer}>
        └ +{model.added} -{model.removed} · {model.files} file{model.files === 1 ? "" : "s"}
      </div>
    </div>
  );
}

function DiffLine({ row }: { row: DiffRow }) {
  return <div className={`${styles.line} ${styles[row.kind]}`}>{row.text}</div>;
}

function buildDiff(diffs: DiffHunk[]) {
  const rows: DiffRow[] = [];
  const paths = new Set<string>();
  let added = 0;
  let removed = 0;
  let previousPath = "";
  for (const diff of diffs) {
    paths.add(diff.path);
    rows.push({ kind: diff.path === previousPath ? "gap" : "path", text: diff.path === previousPath ? "…" : diff.path });
    previousPath = diff.path;
    if (diff.oldText !== null) {
      for (const text of contentLines(diff.oldText)) {
        rows.push({ kind: "del", text });
        removed += 1;
      }
    }
    for (const text of contentLines(diff.newText)) {
      rows.push({ kind: "add", text });
      added += 1;
    }
  }
  return { rows, added, removed, files: paths.size };
}

function contentLines(text: string): string[] {
  if (!text) return [];
  return (text.endsWith("\n") ? text.slice(0, -1) : text).split("\n");
}

function copyText(rows: DiffRow[]): string {
  return rows.map((row) => row.kind === "del"
    ? `- ${row.text}`
    : row.kind === "add"
      ? `+ ${row.text}`
      : row.text).join("\n");
}
