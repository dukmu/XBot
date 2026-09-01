/* Presentation adapted from DeepSeek Harness ui-tool (MIT). */
import { Check, ChevronRight, Circle, CircleDot, FileText, LoaderCircle, Search, Terminal, X } from "lucide-react";
import { memo, useState } from "react";
import type { ToolEntry } from "../state/runtime";
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
          {running ? <LoaderCircle size={14} className="spin" /> : tool.status === "success" ? <Check size={14} /> : <X size={14} />}
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

function ToolBody({ tool, todos }: { tool: ToolEntry; todos: TodoItem[] | null }) {
  if (todos) return <div className="tool-details"><TodoChecklist items={todos} /></div>;
  const args = recordOf(tool.args);
  const command = stringOf(args.command);
  const path = stringOf(args.path);
  const query = stringOf(args.query);
  if (command && /(?:shell|bash|exec|terminal|command)/i.test(tool.name)) {
    return (
      <div className="tool-details"><section className="tool-specialized-card tool-terminal-card">
        <header><Terminal size={14} /><code>{command}</code></header>
        {tool.result !== null && tool.result !== "" && <ToolOutput value={tool.result} />}
        {tool.error && <ToolOutput value={tool.error} label="Error" />}
      </section></div>
    );
  }
  if (path && /(?:file|read|write|edit|patch)/i.test(tool.name)) {
    return (
      <div className="tool-details"><section className="tool-specialized-card tool-file-card">
        <header><FileText size={14} /><span>Path</span><code>{path}</code></header>
        {tool.result !== null && tool.result !== "" && <Detail label="Result" value={tool.result} />}
        {tool.error && <Detail label="Error" value={tool.error} />}
      </section></div>
    );
  }
  if (query && /search/i.test(tool.name)) {
    return (
      <div className="tool-details"><section className="tool-specialized-card tool-search-card">
        <header><Search size={14} /><span>Query</span><code>{query}</code></header>
        {tool.result !== null && tool.result !== "" && <Detail label="Result" value={tool.result} />}
        {tool.error && <Detail label="Error" value={tool.error} />}
      </section></div>
    );
  }
  return (
    <div className="tool-details">
      <Detail label="Arguments" value={tool.args} />
      {tool.result !== null && tool.result !== "" && <Detail label="Result" value={tool.result} />}
      {tool.data !== null && <Detail label="Data" value={tool.data} />}
      {tool.error && <Detail label="Error" value={tool.error} />}
      {tool.images.length > 0 && <Detail label="Images" value={tool.images} />}
    </div>
  );
}

function Detail({ label, value }: { label: string; value: unknown }) {
  return <div className="tool-detail-section"><ToolOutput label={label} value={value} /></div>;
}

function toolSummary(tool: ToolEntry, todos: TodoItem[] | null): string {
  if (tool.status === "denied") return "denied";
  if (tool.status === "error") return "failed";
  if (todos) {
    const completed = todos.filter((item) => item.status === "completed").length;
    const active = todos.find((item) => item.status === "in_progress");
    return active ? `${completed}/${todos.length} done · ${active.content}` : `${completed}/${todos.length} done`;
  }
  const args = recordOf(tool.args);
  return stringOf(args.path) || stringOf(args.command) || stringOf(args.query) || stringOf(args.objective) || tool.status;
}

interface TodoItem { content: string; status: "pending" | "in_progress" | "completed" }

function todoItems(tool: ToolEntry): TodoItem[] | null {
  if (tool.name !== "update_todos") return null;
  const projection = recordOf(tool.data);
  const raw = projection.kind === "todo_snapshot" ? projection.items : null;
  if (!Array.isArray(raw)) return null;
  const items: TodoItem[] = [];
  for (const value of raw) {
    const item = recordOf(value);
    if (typeof item.content !== "string" || !["pending", "in_progress", "completed"].includes(String(item.status))) return null;
    items.push({ content: item.content, status: item.status as TodoItem["status"] });
  }
  return items;
}

function TodoChecklist({ items }: { items: TodoItem[] }) {
  return (
    <section className="todo-checklist" aria-label="Todo checklist">
      <span className="todo-checklist-label">Plan</span>
      {items.length === 0 && <div className="todo-empty">Checklist cleared</div>}
      {items.map((item, index) => (
        <div className={`todo-item todo-${item.status}`} key={`${index}-${item.content}`}>
          {item.status === "completed" ? <Check size={14} /> : item.status === "in_progress" ? <CircleDot size={14} /> : <Circle size={14} />}
          <span>{item.content}</span>
          <small>{item.status === "completed" ? "Done" : item.status === "in_progress" ? "In progress" : "Pending"}</small>
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
