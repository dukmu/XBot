/* Presentation adapted from DeepSeek Harness ui-tool (MIT). */
import { Check, ChevronRight, Circle, CircleDot, LoaderCircle, X } from "lucide-react";
import { memo, useState, type ReactNode } from "react";
import type { TodoItemData } from "../api/types";
import type { ToolEntry } from "../state/runtime";
import { DiffBlock, type DiffHunk } from "./DiffBlock";
import { ToolArtifacts } from "./ToolArtifacts";
import { ToolOutput } from "./ToolOutput";

export const ToolCall = memo(function ToolCall({ tool }: { tool: ToolEntry }) {
  const running = tool.status === "running" || tool.status === "pending";
  const todos = todoItems(tool);
  const [open, setOpen] = useState(false);
  return (
    <details
      className={`tool-block status-${tool.status}`}
      data-state={running ? "running" : tool.status}
      data-tool={tool.name}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>
        <span className="tool-status-icon">
          {running ? <LoaderCircle size={14} className="spin" /> : tool.status === "success" || tool.status === "approved" ? <Check size={14} /> : <X size={14} />}
        </span>
        <span className="tool-name">{tool.name}</span>
        <i className="tool-separator" aria-hidden />
        <span className="tool-summary">{toolSummary(tool, todos)}</span>
        <ChevronRight size={13} className="summary-chevron" />
      </summary>
      {open && <ToolBody tool={tool} todos={todos} />}
    </details>
  );
});

function ToolBody({ tool, todos }: { tool: ToolEntry; todos: TodoItemData[] | null }) {
  const args = recordOf(tool.args);
  const command = stringOf(args.command);
  const path = stringOf(args.path);
  const query = stringOf(args.query);
  const terminalTool = Boolean(command && /(?:shell|bash|exec|terminal|command)/i.test(tool.name));
  const argumentsBlock = <ToolArguments value={tool.args} terminal={terminalTool} />;
  if (todos) return <ToolDetails tool={tool}>{argumentsBlock}<TodoChecklist items={todos} /></ToolDetails>;
  const diff = appliedDiff(tool);
  if (diff) return <ToolDetails tool={tool}>{argumentsBlock}<DiffBlock diffs={[diff]} maxLines={8} /></ToolDetails>;
  if (command && /(?:shell|bash|exec|terminal|command)/i.test(tool.name)) {
    return (
      <ToolDetails tool={tool}>{argumentsBlock}{resultBlock(tool, "tool-terminal-result")}</ToolDetails>
    );
  }
  if (path && /(?:file|read|write|edit|patch)/i.test(tool.name)) {
    return (
      <ToolDetails tool={tool}>{argumentsBlock}{resultBlock(tool, "tool-file-result")}</ToolDetails>
    );
  }
  if (query && /search/i.test(tool.name)) {
    return (
      <ToolDetails tool={tool}>{argumentsBlock}{resultBlock(tool, "tool-search-result")}</ToolDetails>
    );
  }
  return (
    <ToolDetails tool={tool}>
      {argumentsBlock}
      {tool.result !== null && tool.result !== "" && <Detail label="Result" value={tool.result} />}
      {tool.data !== null && <Detail label="Data" value={tool.data} />}
      {tool.error && <Detail label="Error" value={tool.error} />}
      {tool.images.length > 0 && <Detail label="Images" value={tool.images} />}
    </ToolDetails>
  );
}

function ToolDetails({ tool, children }: { tool: ToolEntry; children: ReactNode }) {
  return <div className="tool-details">{children}<ToolArtifacts artifacts={tool.artifacts} /></div>;
}

function appliedDiff(tool: ToolEntry): DiffHunk | null {
  if (tool.name !== "edit" || tool.status !== "success") return null;
  const data = recordOf(tool.data);
  if (data.changed !== true) return null;
  const args = recordOf(tool.args);
  const path = stringOf(args.path);
  if (!path.trim()) return null;
  if (args.mode === "write" && typeof args.content === "string") {
    return { path, oldText: null, newText: args.content };
  }
  if (
    args.mode === "replace"
    && typeof args.old_text === "string"
    && typeof args.new_text === "string"
  ) {
    return { path, oldText: args.old_text, newText: args.new_text };
  }
  return null;
}

function Detail({ label, value }: { label: string; value: unknown }) {
  return <div className="tool-detail-section"><ToolOutput label={label} value={value} /></div>;
}

function resultBlock(tool: ToolEntry, className: string) {
  const hasResult = tool.result !== null && tool.result !== "";
  const hasError = tool.error !== null;
  const placeholder = tool.status === "running" || tool.status === "pending"
    ? "Waiting for output…"
    : "No output";
  return (
    <section className={`tool-specialized-card ${className}`}>
      <Detail label="Result" value={hasResult ? tool.result : placeholder} />
      {hasError && <Detail label="Error" value={tool.error} />}
    </section>
  );
}

function ToolArguments({ value, terminal }: { value: unknown; terminal: boolean }) {
  const args = recordOf(value);
  const entries = Object.entries(args);
  return (
    <details className="tool-arguments" aria-label="Arguments">
      <summary>
        <span>Arguments</span>
        <span className="tool-arguments-count">{entries.length} {entries.length === 1 ? "parameter" : "parameters"}</span>
      </summary>
      <div className="tool-arguments-body">
        {entries.length === 0
          ? <span className="tool-arguments-empty">No arguments</span>
          : entries.map(([name, argument]) => (
            <div className="tool-argument-card" key={name}>
              {terminal && name === "command" && typeof argument === "string"
                ? <CommandArgument value={argument} />
                : <ToolOutput label={name} value={argument} />}
            </div>
          ))}
      </div>
    </details>
  );
}

function CommandArgument({ value }: { value: string }) {
  return (
    <div className="tool-command-argument">
      <span className="tool-argument-label">command</span>
      <pre><code><span className="tool-command-prompt">$ </span>{value}</code></pre>
    </div>
  );
}

function toolSummary(tool: ToolEntry, todos: TodoItemData[] | null): string {
  if (tool.status === "denied") return "denied";
  if (tool.status === "approved") return "approved";
  if (tool.status === "error") return "failed";
  if (todos) {
    const completed = todos.filter((item) => item.status === "completed").length;
    const active = todos.find((item) => item.status === "in_progress");
    return active
      ? `${completed}/${todos.length} done · ${active.activeForm || active.subject}`
      : `${completed}/${todos.length} done`;
  }
  const args = recordOf(tool.args);
  return stringOf(args.path) || stringOf(args.command) || stringOf(args.query) || stringOf(args.subject) || stringOf(args.objective) || tool.status;
}

function todoItems(tool: ToolEntry): TodoItemData[] | null {
  const projection = recordOf(tool.data);
  if (projection.kind !== "todo_snapshot") return null;
  const raw = projection.tasks;
  if (!Array.isArray(raw)) return null;
  const items: TodoItemData[] = [];
  for (const value of raw) {
    const item = recordOf(value);
    if (typeof item.subject !== "string" || !["pending", "in_progress", "completed"].includes(String(item.status))) return null;
    const activeForm = stringOf(item.activeForm);
    const owner = stringOf(item.owner);
    items.push({
      id: stringOf(item.id),
      subject: item.subject,
      status: item.status as TodoItemData["status"],
      blocks: arrayOfStrings(item.blocks),
      blockedBy: arrayOfStrings(item.blockedBy),
      ...(activeForm ? { activeForm } : {}),
      ...(owner ? { owner } : {}),
    });
  }
  return items;
}

function arrayOfStrings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((entry): entry is string => typeof entry === "string") : [];
}

function TodoChecklist({ items }: { items: TodoItemData[] }) {
  return (
    <section className="todo-checklist" aria-label="Task list">
      <span className="todo-checklist-label">Tasks</span>
      {items.length === 0 && <div className="todo-empty">No tasks</div>}
      {items.map((item) => (
        <div className={`todo-item todo-${item.status}`} key={item.id || item.subject}>
          {item.status === "completed" ? <Check size={14} /> : item.status === "in_progress" ? <CircleDot size={14} /> : <Circle size={14} />}
          <span>#{item.id} {item.subject}</span>
          <small>{item.status === "completed" ? "Done" : item.status === "in_progress" ? (item.activeForm || "In progress") : "Pending"}</small>
        </div>
      ))}
    </section>
  );
}

function recordOf(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function stringOf(value: unknown): string {
  return typeof value === "string" ? value : "";
}
