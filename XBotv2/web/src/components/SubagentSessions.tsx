/* Subagent browsing ported from DeepSeek Harness (MIT): the tree lists child
 * sessions with their state and token use, nests descendants, and can collapse
 * a branch.  XBot threads carry the same facts in ThreadSummary
 * (parent_thread_id / turn_status / usage). */
import { ChevronDown, ChevronRight, GitBranch } from "lucide-react";
import { useMemo, useState } from "react";
import type { ThreadSummary } from "../api/types";

export interface SubagentNode {
  thread: ThreadSummary;
  children: SubagentNode[];
}

/** Build the child tree from parent_thread_id, preserving the given order. */
export function subagentTree(threads: readonly ThreadSummary[]): SubagentNode[] {
  const nodes = new Map<string, SubagentNode>();
  for (const thread of threads) {
    if (thread.kind !== "subagent") continue;
    nodes.set(thread.thread_id, { thread, children: [] });
  }
  const roots: SubagentNode[] = [];
  for (const node of nodes.values()) {
    const parent = nodes.get(node.thread.parent_thread_id);
    // A grandchild whose parent is not in the page is shown at the top level
    // rather than dropped, so no session is unreachable.
    if (parent && parent !== node) parent.children.push(node);
    else roots.push(node);
  }
  return roots;
}

function stateLabel(thread: ThreadSummary): string {
  if (thread.turn_status === "running") return "running";
  return thread.status === "active" ? "not running" : "finished";
}

function tokenLabel(thread: ThreadSummary): string {
  return `${thread.usage.total_tokens.toLocaleString()} tok`;
}

function NodeRow({
  node,
  depth,
  currentThreadId,
  collapsed,
  onToggle,
  onSelect,
}: {
  node: SubagentNode;
  depth: number;
  currentThreadId: string;
  collapsed: ReadonlySet<string>;
  onToggle: (threadId: string) => void;
  onSelect: (thread: ThreadSummary) => void;
}) {
  const thread = node.thread;
  const selected = thread.thread_id === currentThreadId;
  const isCollapsed = collapsed.has(thread.thread_id);
  const label = thread.agent || thread.thread_id;
  const summary = `${label} · ${stateLabel(thread)} · ${tokenLabel(thread)}`;
  return (
    <li role="none">
      <div className="subagent-row" data-depth={depth} data-selected={selected || undefined}>
        {node.children.length > 0 ? (
          <button
            type="button"
            className="subagent-twisty"
            aria-label={isCollapsed ? `Expand ${label} descendants` : `Collapse ${label} descendants`}
            aria-expanded={!isCollapsed}
            onClick={() => onToggle(thread.thread_id)}
          >
            {isCollapsed ? <ChevronRight size={13} /> : <ChevronDown size={13} />}
          </button>
        ) : (
          <span className="subagent-twisty-spacer" aria-hidden />
        )}
        <button
          type="button"
          role="treeitem"
          aria-level={depth + 1}
          aria-selected={selected}
          aria-expanded={node.children.length > 0 ? !isCollapsed : undefined}
          className={`subagent-node ${selected ? "selected" : ""}`}
          data-testid={`subagent-${thread.thread_id}`}
          onClick={() => onSelect(thread)}
        >
          <GitBranch size={13} />
          <span className="subagent-node-copy">
            <b>{thread.thread_id}</b>
            <small>{summary}</small>
          </span>
          <span className={`subagent-state ${thread.turn_status === "running" ? "running" : ""}`}>
            {stateLabel(thread)}
          </span>
        </button>
      </div>
      {node.children.length > 0 && !isCollapsed && (
        <ul role="group" className="subagent-children">
          {node.children.map((child) => (
            <NodeRow
              key={child.thread.thread_id}
              node={child}
              depth={depth + 1}
              currentThreadId={currentThreadId}
              collapsed={collapsed}
              onToggle={onToggle}
              onSelect={onSelect}
            />
          ))}
        </ul>
      )}
    </li>
  );
}

export function SubagentSessions({
  threads,
  currentThreadId,
  onSelect,
  title = "Subagent sessions",
  defaultOpen = true,
}: {
  threads: readonly ThreadSummary[];
  currentThreadId: string;
  onSelect: (thread: ThreadSummary) => void;
  title?: string;
  /** The main transcript shows the count button; a viewed thread opens the tree. */
  defaultOpen?: boolean;
}) {
  const roots = useMemo(() => subagentTree(threads), [threads]);
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(() => new Set<string>());
  const [open, setOpen] = useState(defaultOpen);
  if (roots.length === 0) return null;
  const total = threads.filter((thread) => thread.kind === "subagent").length;
  if (!open) {
    return (
      <section className="subagent-sessions is-collapsed" aria-label={title}>
        <button
          type="button"
          className="subagent-sessions-toggle"
          aria-expanded={false}
          onClick={() => setOpen(true)}
        >
          <GitBranch size={13} />
          {total} subagent{total === 1 ? "" : "s"}
        </button>
      </section>
    );
  }
  const toggle = (threadId: string) => {
    setCollapsed((current) => {
      const next = new Set(current);
      if (next.has(threadId)) next.delete(threadId);
      else next.add(threadId);
      return next;
    });
  };
  return (
    <section className="subagent-sessions" aria-label={title}>
      <header className="subagent-sessions-head">
        <strong>{title}</strong>
        <button
          type="button"
          className="subagent-sessions-toggle"
          aria-expanded
          onClick={() => setOpen(false)}
        >
          <small>{total} subagent{total === 1 ? "" : "s"}</small>
        </button>
      </header>
      <ul role="tree" aria-label={title} className="subagent-tree">
        {roots.map((node) => (
          <NodeRow
            key={node.thread.thread_id}
            node={node}
            depth={0}
            currentThreadId={currentThreadId}
            collapsed={collapsed}
            onToggle={toggle}
            onSelect={onSelect}
          />
        ))}
      </ul>
    </section>
  );
}
