/* Trajectory pane adapted from DeepSeek Harness ui-trajectory (MIT): the same
 * toolbar and record table, over XBot's own durable trajectory items. */
import { useMemo, useState } from "react";
import { ChevronRight, Search } from "lucide-react";
import type { TrajectoryItem } from "../api/types";

/** Ported toolbar copy. */
export const TRAJECTORY_TOOLBAR = "Trajectory toolbar";
export const TRAJECTORY_TIMELINE = "Trajectory timeline";
export const TRAJECTORY_SEARCH = "Search trajectory";
export const DURATION_LABEL = "Use actual duration";
export const TURNS_LABEL = "Collapse turns";
export const CALLS_LABEL = "Collapse calls";

/** One rendered table row. */
export interface TrajectoryRow {
  /** Trajectory position, the row's identity. */
  position: number;
  /** `SYSTEM` / `USER` / `ASSISTANT` / `TOOL` / event provenance. */
  role: string;
  /** The turn this record belongs to, 0 before the first turn boundary. */
  turn: number;
  /** 1-based assistant request inside its turn, 0 for other roles. */
  request: number;
  /** Group label shown in the role cell (`Turn 1`, `Request #2`, …). */
  group: string;
  /** Detail shown in the content cell. */
  detail: string;
  /** The tool call this row addresses, when it is a tool result. */
  toolCallId: string;
  /** Timing line, when the record carries one. */
  timing: string;
  /** Whether the row is a tool result (the only rows the pane can select). */
  tool: boolean;
}

/** Ported timing line: `Total 1,542 ms · TTFT 368 ms · Decoding 1,174 ms`. */
export function formatTiming(timing: Record<string, number> | null | undefined): string {
  if (!timing) return "";
  const parts: string[] = [];
  const total = timing.llm_ms ?? timing.duration_ms;
  if (typeof total === "number") parts.push(`Total ${Math.round(total).toLocaleString("en-US")} ms`);
  if (typeof timing.ttft_ms === "number") parts.push(`TTFT ${Math.round(timing.ttft_ms).toLocaleString("en-US")} ms`);
  if (typeof timing.decode_ms === "number") parts.push(`Decoding ${Math.round(timing.decode_ms).toLocaleString("en-US")} ms`);
  return parts.join(" · ");
}

/** Collapse a record's text to one clipped line for the content cell. */
function clip(text: string, limit = 400): string {
  const flat = text.replace(/\s+/g, " ").trim();
  return flat.length > limit ? `${flat.slice(0, limit)}…` : flat;
}

function toolCallSummary(calls: unknown): string {
  if (!Array.isArray(calls) || calls.length === 0) return "";
  return calls
    .map((call) => {
      const value = typeof call === "object" && call !== null ? call as Record<string, unknown> : {};
      const name = typeof value.name === "string" ? value.name : "tool";
      const args = value.arguments ?? value.args ?? {};
      return `${name}${JSON.stringify(args)}`;
    })
    .join(" ");
}

/**
 * Project the durable trajectory into the ported table's rows: turn and request
 * numbering from the turn boundaries the trajectory records, and one row per
 * record with the timing line the ported table shows on hover.
 */
export function trajectoryRows(items: TrajectoryItem[]): TrajectoryRow[] {
  const rows: TrajectoryRow[] = [];
  let turn = 0;
  let request = 0;
  for (const item of items) {
    if (item.kind === "event") {
      // Lifecycle events drive the numbering; the table lists conversation
      // records only, as the ported one does (SYSTEM / USER / ASSISTANT / TOOL).
      if (item.event === "turn_started") {
        const data = item.data as Record<string, unknown>;
        turn = typeof data.turn === "number" ? data.turn : turn + 1;
        request = 0;
      }
      continue;
    }
    if (item.kind === "surface_replace") {
      rows.push({
        position: item.position,
        role: item.operation,
        turn,
        request: 0,
        group: turn > 0 ? `Turn ${turn}` : "",
        detail: clip(item.summary || item.messages.map((message) => message.content).join(" ")),
        toolCallId: "",
        timing: "",
        tool: false,
      });
      continue;
    }
    const message = item.message;
    const role = message.role.toUpperCase();
    if (message.role === "assistant") request += 1;
    const calls = toolCallSummary(message.tool_calls);
    rows.push({
      position: item.position,
      role,
      turn,
      request: message.role === "assistant" ? request : 0,
      group: message.role === "assistant"
        ? `Request #${request}`
        : turn > 0 ? `Turn ${turn}` : "",
      detail: [clip(message.content), calls, message.error ? `Error: ${message.error.message ?? ""}` : ""]
        .filter(Boolean)
        .join(" → "),
      toolCallId: message.tool_call_id || "",
      timing: formatTiming(message.timing),
      tool: message.role === "tool",
    });
  }
  return rows;
}

/**
 * Apply the toolbar to the projected rows: the search box filters, collapsing
 * turns keeps one summary row per turn, and collapsing calls drops tool rows
 * (their count stays on the request that made them).
 */
export function visibleRows(rows: TrajectoryRow[], options: {
  query: string;
  collapseTurns: boolean;
  collapseCalls: boolean;
}): TrajectoryRow[] {
  let visible = rows;
  if (options.collapseCalls) visible = visible.filter((row) => !row.tool);
  const query = options.query.trim().toLowerCase();
  if (query) {
    visible = visible.filter((row) => (
      `${row.role} ${row.group} ${row.detail}`.toLowerCase().includes(query)
    ));
  }
  if (!options.collapseTurns) return visible;
  const turns = new Map<number, { rows: TrajectoryRow[]; first: TrajectoryRow }>();
  const order: number[] = [];
  for (const row of visible) {
    if (row.turn === 0) continue;
    let group = turns.get(row.turn);
    if (!group) {
      group = { rows: [], first: row };
      turns.set(row.turn, group);
      order.push(row.turn);
    }
    group.rows.push(row);
  }
  if (turns.size === 0) return visible;
  const summary: TrajectoryRow[] = order.map((turn) => {
    const group = turns.get(turn)!;
    const requests = group.rows.filter((row) => row.request > 0).length;
    return {
      ...group.first,
      role: "TURN",
      group: `Turn ${turn}`,
      detail: `${group.rows.length} records · ${requests} requests`,
      timing: "",
      tool: false,
    };
  });
  return [...visible.filter((row) => row.turn === 0), ...summary];
}

/**
 * The Trajectory pane: the ported toolbar (a trajectory search box and the
 * duration / turns / calls controls) over one record table.
 */
export function TrajectoryPane({
  items,
  selectedPosition,
  onSelect,
  hasOlder,
  loadingOlder,
  onLoadOlder,
}: {
  items: TrajectoryItem[];
  selectedPosition?: number;
  onSelect?: (row: TrajectoryRow) => void;
  hasOlder?: boolean;
  loadingOlder?: boolean;
  onLoadOlder?: () => Promise<void>;
}) {
  const [query, setQuery] = useState("");
  const [duration, setDuration] = useState(true);
  const [collapseTurns, setCollapseTurns] = useState(false);
  const [collapseCalls, setCollapseCalls] = useState(false);
  const rows = useMemo(
    () => visibleRows(trajectoryRows(items), { query, collapseTurns, collapseCalls }),
    [items, query, collapseTurns, collapseCalls],
  );

  return (
    <div className="trajectory-pane">
      <div className="trajectory-toolbar" role="toolbar" aria-label={TRAJECTORY_TOOLBAR}>
        <button
          type="button"
          className="trajectory-toggle"
          aria-label={DURATION_LABEL}
          aria-pressed={duration}
          onClick={() => setDuration((value) => !value)}
        >
          Duration
        </button>
        <button
          type="button"
          className="trajectory-toggle"
          aria-label={TURNS_LABEL}
          aria-pressed={collapseTurns}
          onClick={() => setCollapseTurns((value) => !value)}
        >
          Turns
        </button>
        <button
          type="button"
          className="trajectory-toggle"
          aria-label={CALLS_LABEL}
          aria-pressed={collapseCalls}
          onClick={() => setCollapseCalls((value) => !value)}
        >
          Calls
        </button>
        <label className="trajectory-search">
          <Search size={13} aria-hidden />
          <input
            type="search"
            aria-label={TRAJECTORY_SEARCH}
            placeholder={TRAJECTORY_SEARCH}
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
        </label>
      </div>
      <div className="trajectory-scroll" role="region" aria-label={TRAJECTORY_TIMELINE}>
        {(hasOlder || onLoadOlder) && (
          <button
            type="button"
            className="trajectory-older"
            disabled={loadingOlder}
            onClick={() => void onLoadOlder?.()}
          >
            <ChevronRight size={13} className="trajectory-older-icon" /> Older records
          </button>
        )}
        <table className="trajectory-table">
          <tbody>
            {rows.map((row) => (
              <tr
                key={row.position}
                aria-selected={selectedPosition === row.position || undefined}
                data-tool={row.tool ? "" : undefined}
                onClick={() => onSelect?.(row)}
              >
                <th scope="row" title={row.tool ? "Select this call" : undefined}>
                  {row.group && <span className="trajectory-group">{row.group}</span>}
                  <span className="trajectory-role">{row.role}</span>
                </th>
                <td>
                  <span className="trajectory-detail">{row.detail}</span>
                  {duration && row.timing && <span className="trajectory-timing">{row.timing}</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {rows.length === 0 && <p className="trajectory-empty">No matching records</p>}
      </div>
    </div>
  );
}
