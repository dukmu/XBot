import { memo, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Brain, Check, ChevronRight, Circle, CircleAlert, CircleDot, Clipboard, ClipboardCheck, ChevronUp, LoaderCircle, RotateCcw, Terminal, UserRound, X } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { TimelineEntry, ToolEntry } from "../state/runtime";

const TIMELINE_WINDOW = 160;
const TIMELINE_BATCH = 80;

interface TimelineProps {
  entries: TimelineEntry[];
  assistantDraft: Extract<TimelineEntry, { kind: "message" }> | null;
  turnRunning: boolean;
  onRetry: () => Promise<void>;
  hasOlder: boolean;
  loadingOlder: boolean;
  onLoadOlder: () => Promise<void>;
}

export const Timeline = memo(function Timeline({
  entries,
  assistantDraft,
  turnRunning,
  onRetry,
  hasOlder,
  loadingOlder,
  onLoadOlder,
}: TimelineProps) {
  const viewport = useRef<HTMLDivElement>(null);
  const shouldFollow = useRef(true);
  const previousLength = useRef(0);
  const pendingPrependHeight = useRef<number | null>(null);
  const [windowRange, setWindowRange] = useState({ start: -1, end: -1 });
  const [showLatest, setShowLatest] = useState(false);
  const latestAssistant = latestAssistantId(entries);
  const range = windowRange.start < 0
    ? { start: Math.max(0, entries.length - TIMELINE_WINDOW), end: entries.length }
    : {
      start: Math.min(windowRange.start, entries.length),
      end: Math.min(windowRange.end, entries.length),
    };
  const visibleEntries = useMemo(
    () => entries.slice(range.start, range.end),
    [entries, range.start, range.end],
  );

  useLayoutEffect(() => {
    const previous = previousLength.current;
    previousLength.current = entries.length;
    setWindowRange((current) => {
      if (current.start < 0) {
        return range;
      }
      if (entries.length > previous && shouldFollow.current) {
        return {
          start: Math.max(0, entries.length - TIMELINE_WINDOW),
          end: entries.length,
        };
      }
      if (entries.length < current.end) {
        return {
          start: Math.min(current.start, entries.length),
          end: entries.length,
        };
      }
      return current;
    });
  }, [entries.length, range.start, range.end]);

  useLayoutEffect(() => {
    const previousHeight = pendingPrependHeight.current;
    const element = viewport.current;
    if (previousHeight === null || !element) return;
    pendingPrependHeight.current = null;
    element.scrollTop += element.scrollHeight - previousHeight;
  }, [entries.length, range.start, range.end]);

  const loadEarlier = () => {
    const element = viewport.current;
    if (!element || (range.start <= 0 && !hasOlder)) return;
    shouldFollow.current = false;
    setShowLatest(true);
    pendingPrependHeight.current = element.scrollHeight;
    if (range.start <= 0) {
      void onLoadOlder();
      return;
    }
    setWindowRange((current) => {
      const start = Math.max(0, current.start - TIMELINE_BATCH);
      return { start, end: Math.min(entries.length, start + TIMELINE_WINDOW) };
    });
  };

  const scrollToLatest = () => {
    shouldFollow.current = true;
    setShowLatest(false);
    setWindowRange({
      start: Math.max(0, entries.length - TIMELINE_WINDOW),
      end: entries.length,
    });
    viewport.current?.scrollTo({ top: viewport.current.scrollHeight, behavior: "smooth" });
  };

  useEffect(() => {
    if (shouldFollow.current) viewport.current?.scrollTo({ top: viewport.current.scrollHeight });
  }, [entries, assistantDraft?.content, assistantDraft?.reasoning]);

  return (
    <div
      className="timeline"
      ref={viewport}
      onScroll={(event) => {
        const element = event.currentTarget;
        const following = element.scrollHeight - element.scrollTop - element.clientHeight < 120;
        shouldFollow.current = following;
        setShowLatest(!following || range.end < entries.length);
      }}
    >
      <div className="timeline-inner">
        {(range.start > 0 || hasOlder) && (
          <button className="timeline-older" type="button" disabled={loadingOlder} onClick={loadEarlier}>
            {loadingOlder ? <LoaderCircle size={14} className="spin" /> : <ChevronUp size={14} />} Older messages
          </button>
        )}
        {visibleEntries.map((entry) => {
          if (entry.kind === "message") {
            return <MessageBlock key={entry.id} entry={entry} canRetry={!turnRunning && entry.role === "assistant" && entry.id === latestAssistant} onRetry={onRetry} />;
          }
          if (entry.kind === "tool") return <ToolBlock key={entry.id} tool={entry} />;
          if (entry.kind === "runtime") {
            return (
              <details key={entry.id} className="runtime-context">
                <summary>
                  <Terminal size={14} />
                  <span>Injected context</span>
                  <code>{entry.source} · {entry.event}</code>
                  <ChevronRight size={13} className="summary-chevron" />
                </summary>
                <div className="markdown-body"><ReactMarkdown remarkPlugins={[remarkGfm]}>{entry.content}</ReactMarkdown></div>
              </details>
            );
          }
          return (
            <div key={entry.id} className={`notice-row ${entry.level}`}>
              {entry.level === "error" ? <CircleAlert size={14} /> : <Terminal size={14} />}
              <span>{entry.content}</span>
            </div>
          );
        })}
        {assistantDraft && (
          <MessageBlock entry={assistantDraft} canRetry={false} onRetry={onRetry} />
        )}
        {turnRunning && !assistantDraft && (
          <div className="turn-pending"><LoaderCircle size={15} className="spin" /> Working</div>
        )}
      </div>
      {showLatest && (
        <button className="timeline-latest" type="button" aria-label="Jump to latest activity" onClick={scrollToLatest}>
          <ChevronRight size={14} className="timeline-latest-icon" /> Latest activity
        </button>
      )}
    </div>
  );
}, (previous, next) => (
  previous.entries === next.entries
  && previous.assistantDraft === next.assistantDraft
  && previous.turnRunning === next.turnRunning
  && previous.hasOlder === next.hasOlder
  && previous.loadingOlder === next.loadingOlder
));

const MessageBlock = memo(function MessageBlock({
  entry,
  canRetry,
  onRetry,
}: {
  entry: Extract<TimelineEntry, { kind: "message" }>;
  canRetry: boolean;
  onRetry: () => Promise<void>;
}) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    if (!entry.content || !navigator.clipboard) return;
    await navigator.clipboard.writeText(entry.content);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1400);
  };
  return (
    <article className={`message-block ${entry.role}`}>
      <div className="message-author">
        {entry.role === "user" ? <UserRound size={14} /> : <span className="xbot-glyph">X</span>}
        <span>{entry.role === "user" ? "You" : "XBot"}</span>
      </div>
      {entry.reasoning && (
        <details className="reasoning-block" open={entry.streaming}>
          <summary>
            {entry.streaming ? <LoaderCircle size={14} className="spin" /> : <Brain size={14} />}
            Thinking
            <ChevronRight size={13} className="summary-chevron" />
          </summary>
          <div className="reasoning-content">{entry.reasoning}</div>
        </details>
      )}
      {entry.content && (
        entry.streaming
          ? <div className="streaming-content">{entry.content}</div>
          : <div className="markdown-body"><ReactMarkdown remarkPlugins={[remarkGfm]}>{entry.content}</ReactMarkdown></div>
      )}
      {!entry.streaming && entry.content && (
        <div className="message-actions">
          <button className="icon-button small" title={copied ? "Copied" : "Copy message"} aria-label={copied ? "Copied" : "Copy message"} onClick={() => void copy()}>
            {copied ? <ClipboardCheck size={13} /> : <Clipboard size={13} />}
          </button>
          {entry.role === "assistant" && canRetry && (
            <button className="icon-button small" title="Regenerate response" aria-label="Regenerate response" onClick={() => void onRetry()}>
              <RotateCcw size={13} />
            </button>
          )}
        </div>
      )}
      {entry.images.length > 0 && (
        <div className="message-images">
          {entry.images.map((image, index) => image.src ? (
            <a key={`${image.label}-${index}`} href={image.href || image.src} target="_blank" rel="noreferrer">
              <img src={image.src} alt={image.label} loading="lazy" />
            </a>
          ) : image.href ? (
            <a className="message-image-reference" key={`${image.label}-${index}`} href={image.href} target="_blank" rel="noreferrer">{image.label}</a>
          ) : (
            <div className="message-image-reference" key={`${image.label}-${index}`}>{image.label}</div>
          ))}
        </div>
      )}
      {entry.streaming && !entry.content && !entry.reasoning && (
        <div className="assistant-pending"><i /><i /><i /></div>
      )}
    </article>
  );
}, (previous, next) => previous.entry === next.entry && previous.canRetry === next.canRetry);

const ToolBlock = memo(function ToolBlock({ tool }: { tool: ToolEntry }) {
  const running = tool.status === "running" || tool.status === "pending";
  const todos = todoItems(tool);
  const [open, setOpen] = useState(false);
  return (
    <details
      className={`tool-block status-${tool.status}`}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>
        <span className="tool-status-icon">
          {running ? <LoaderCircle size={14} className="spin" /> : tool.status === "success" ? <Check size={14} /> : <X size={14} />}
        </span>
        <span className="tool-name">{tool.name}</span>
        <span className="tool-summary">{toolSummary(tool)}</span>
        <ChevronRight size={13} className="summary-chevron" />
      </summary>
      {open && (
        <div className="tool-details">
          {todos ? <TodoChecklist items={todos} /> : <Detail label="Arguments" value={tool.args} />}
          {tool.result !== null && tool.result !== "" && <Detail label="Result" value={tool.result} />}
          {tool.data !== null && <Detail label="Data" value={tool.data} />}
          {tool.error && <Detail label="Error" value={tool.error} />}
          {tool.images.length > 0 && <Detail label="Images" value={tool.images} />}
        </div>
      )}
    </details>
  );
});

function Detail({ label, value }: { label: string; value: unknown }) {
  return (
    <div className="tool-detail-section">
      <span>{label}</span>
      <pre>{formatValue(value)}</pre>
    </div>
  );
}

function toolSummary(tool: ToolEntry): string {
  if (tool.status === "denied") return "denied";
  if (tool.status === "error") return "failed";
  const args = tool.args && typeof tool.args === "object" ? tool.args as Record<string, unknown> : {};
  const todos = todoItems(tool);
  if (todos) {
    const completed = todos.filter((item) => item.status === "completed").length;
    const active = todos.find((item) => item.status === "in_progress");
    return active ? `${completed}/${todos.length} done · ${active.content}` : `${completed}/${todos.length} done`;
  }
  const candidate = args.path || args.command || args.query || args.objective;
  return typeof candidate === "string" ? candidate : tool.status;
}

interface TodoItem {
  content: string;
  status: "pending" | "in_progress" | "completed";
}

function todoItems(tool: ToolEntry): TodoItem[] | null {
  if (tool.name !== "update_todos") return null;
  const projection = tool.data && typeof tool.data === "object" && !Array.isArray(tool.data)
    ? tool.data as Record<string, unknown>
    : null;
  const raw = projection?.kind === "todo_snapshot" ? projection.items : null;
  if (!Array.isArray(raw)) return null;
  const items: TodoItem[] = [];
  for (const value of raw) {
    if (!value || typeof value !== "object" || Array.isArray(value)) return null;
    const item = value as Record<string, unknown>;
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

function formatValue(value: unknown): string {
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function latestAssistantId(entries: TimelineEntry[]): string {
  for (let index = entries.length - 1; index >= 0; index -= 1) {
    const entry = entries[index];
    if (entry.kind === "message" && entry.role === "assistant" && !entry.streaming) return entry.id;
  }
  return "";
}
