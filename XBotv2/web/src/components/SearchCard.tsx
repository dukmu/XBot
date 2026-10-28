/*
 * Search card adapted from DeepSeek Harness ui-primitives/SearchBlock.tsx and
 * ui-tool/client/tool/models/search-card-model.ts (MIT): the same banner
 * summary, per-file match groups, head/tail height cap and copy control, over
 * XBot's own search result shape.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { ChevronRight } from "lucide-react";

/** Rows a chat row shows before the middle collapses (the ported chat cap). */
export const CHAT_SEARCH_MAX_LINES = 8;

export interface SearchFileGroup {
  path: string;
  matches: { lineNumber: number; line: string }[];
}

export type SearchCardProps =
  | { kind: "matches"; truncated: boolean; files: SearchFileGroup[]; maxLines?: number }
  | { kind: "paths"; truncated: boolean; paths: string[]; maxLines?: number };

type Row =
  | { type: "file"; path: string; count: number; index: number; collapsed: boolean }
  | { type: "match"; lineNumber: number; line: string; key: string; fileIndex: number }
  | { type: "path"; path: string };

/**
 * Head/tail height-cap arithmetic, as the ported primitives share it: `ceil(cap
 * / 2)` head rows and the remainder as tail rows, nothing hidden within the cap.
 */
export function headTailCap(total: number, maxLines: number, expanded: boolean) {
  const hidden = total - maxLines;
  const headLines = Math.ceil(maxLines / 2);
  return { hidden, capped: hidden > 0 && !expanded, headLines, tailLines: maxLines - headLines };
}

/** The banner summary: what the card holds, and that it is a capped first page. */
export function searchSummary(props: SearchCardProps): string {
  const shown = props.kind === "paths"
    ? props.paths.length
    : props.files.reduce((sum, file) => sum + file.matches.length, 0);
  const count = `${shown} ${shown === 1 ? "match" : "matches"}`;
  if (props.kind === "paths") {
    return `${shown} ${shown === 1 ? "path" : "paths"}`;
  }
  const files = `${props.files.length} ${props.files.length === 1 ? "file" : "files"}`;
  return `${props.truncated ? `first ${count}` : count} · ${files}`;
}

/** The plain-text form the copy control writes. */
export function searchCopyText(props: SearchCardProps): string {
  if (props.kind === "paths") return props.paths.join("\n");
  return props.files
    .map((file) => [file.path, ...file.matches.map((match) => `${match.lineNumber}: ${match.line}`)].join("\n"))
    .join("\n\n");
}

function toRows(props: SearchCardProps, collapsed: ReadonlySet<number>): Row[] {
  if (props.kind === "paths") return props.paths.map((path): Row => ({ type: "path", path }));
  const rows: Row[] = [];
  props.files.forEach((file, index) => {
    const isCollapsed = collapsed.has(index);
    rows.push({ type: "file", path: file.path, count: file.matches.length, index, collapsed: isCollapsed });
    if (isCollapsed) return;
    for (const match of file.matches) {
      rows.push({
        type: "match",
        lineNumber: match.lineNumber,
        line: match.line,
        key: `${index}:${match.lineNumber}`,
        fileIndex: index,
      });
    }
  });
  return rows;
}

function rowKey(row: Row): string {
  switch (row.type) {
    case "match": return `match:${row.key}`;
    case "file": return `file:${row.index}`;
    case "path": return `path:${row.path}`;
  }
}

/**
 * One completed search, as the ported card draws it: a banner summary with a
 * copy control, then matches grouped per file (each group collapsible) or a flat
 * path list. Long lists keep a head and a tail slice with the middle behind an
 * expand control.
 */
export function SearchCard(props: SearchCardProps) {
  const { truncated, maxLines = CHAT_SEARCH_MAX_LINES } = props;
  const [expanded, setExpanded] = useState(false);
  const [collapsed, setCollapsed] = useState<ReadonlySet<number>>(() => new Set());
  const [copied, setCopied] = useState(false);
  const pending = useRef(false);
  const timer = useRef<number | null>(null);

  useEffect(() => () => {
    if (timer.current !== null) window.clearTimeout(timer.current);
  }, []);

  const copy = useCallback(async () => {
    if (copied || pending.current || !navigator.clipboard) return;
    pending.current = true;
    try {
      await navigator.clipboard.writeText(searchCopyText(props));
      setCopied(true);
      timer.current = window.setTimeout(() => {
        timer.current = null;
        setCopied(false);
      }, 1000);
    } catch {
      // A denied clipboard leaves the control available for another try.
    } finally {
      pending.current = false;
    }
  }, [copied, props]);

  const toggleFile = useCallback((index: number) => {
    setCollapsed((previous) => {
      const next = new Set(previous);
      if (next.has(index)) next.delete(index);
      else next.add(index);
      return next;
    });
  }, []);

  const rows = toRows(props, collapsed);
  const { hidden, capped, headLines, tailLines } = headTailCap(rows.length, maxLines, expanded);
  const head = capped ? rows.slice(0, headLines) : rows;
  const naturalTail = capped ? rows.slice(rows.length - tailLines) : [];
  // A tail that starts inside a file's matches needs that file's header above
  // it; the header costs a tail slot, so the match it introduces joins the
  // hidden middle and the visible rows stay at the cap.
  const tailLead = naturalTail[0];
  const tailHeader = tailLead?.type === "match"
    && !head.some((row) => row.type === "file" && row.index === tailLead.fileIndex)
    ? rows.find((row): row is Extract<Row, { type: "file" }> => (
      row.type === "file" && row.index === tailLead.fileIndex
    ))
    : undefined;
  const tail = tailHeader === undefined ? naturalTail : naturalTail.slice(1);

  const renderRow = (row: Row) => {
    if (row.type === "path") return <div className="search-line">{row.path}</div>;
    if (row.type === "match") {
      return (
        <div className="search-line">
          <span className="search-line-number">{row.lineNumber}: </span>
          {row.line}
        </div>
      );
    }
    return (
      <button
        type="button"
        className="search-file"
        aria-expanded={!row.collapsed}
        onClick={() => toggleFile(row.index)}
      >
        <ChevronRight size={12} className={row.collapsed ? "" : "open"} />
        <span className="search-path">{row.path}</span>
        <span className="search-count">{row.count}</span>
      </button>
    );
  };

  const empty = rows.length === 0;
  return (
    <div className="search-card" data-search={props.kind}>
      <div className="search-head">
        <span className="search-summary">{searchSummary(props)}</span>
        {!empty && (
          <button type="button" className="search-copy" onClick={() => void copy()}>
            {copied ? "Copied" : "Copy"}
          </button>
        )}
      </div>
      {empty
        ? <div className="search-empty">No results</div>
        : (
          <div className="search-body">
            {head.map((row) => <div key={rowKey(row)}>{renderRow(row)}</div>)}
            {hidden > 0 && (
              <button
                type="button"
                className="search-expand"
                aria-expanded={expanded}
                aria-label={expanded ? "Collapse results" : `Expand the remaining ${hidden} rows`}
                onClick={() => setExpanded((value) => !value)}
              >
                {expanded ? "Collapse" : `… ${hidden} more ${hidden === 1 ? "row" : "rows"}`}
              </button>
            )}
            {tailHeader !== undefined && <div key={`tailHeader:${rowKey(tailHeader)}`}>{renderRow(tailHeader)}</div>}
            {tail.map((row) => <div key={rowKey(row)}>{renderRow(row)}</div>)}
          </div>
        )}
      {truncated && (
        // The ported card folds the pre-cap total into its summary; XBot's
        // search reports only that it capped, and no locator for the rest.
        <div className="search-capped" role="status">
          The search stopped at its result limit; more matches exist.
        </div>
      )}
    </div>
  );
}

/** Derive the card from one search tool call's structured result, or null. */
export function searchCard(tool: { name: string; data: unknown }): SearchCardProps | null {
  if (!/search|grep|glob|find/i.test(tool.name)) return null;
  const data = recordOf(tool.data);
  const truncated = data.truncated === true;
  const paths = data.files;
  if (Array.isArray(paths) && paths.every((path) => typeof path === "string")) {
    return { kind: "paths", truncated, paths: paths as string[] };
  }
  const matches = data.matches;
  if (!Array.isArray(matches)) return null;
  const files: SearchFileGroup[] = [];
  const byPath = new Map<string, SearchFileGroup>();
  for (const value of matches) {
    const match = recordOf(value);
    const path = stringOf(match.path);
    const line = typeof match.line === "number" ? match.line : Number(match.line);
    if (!path || !Number.isFinite(line)) return null;
    let group = byPath.get(path);
    if (!group) {
      group = { path, matches: [] };
      byPath.set(path, group);
      files.push(group);
    }
    group.matches.push({ lineNumber: line, line: stringOf(match.text) });
  }
  return { kind: "matches", truncated, files };
}

function recordOf(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function stringOf(value: unknown): string {
  return typeof value === "string" ? value : "";
}
