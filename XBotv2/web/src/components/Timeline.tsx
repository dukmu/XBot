import { memo, useEffect, useRef, useState } from "react";
import { Brain, Check, ChevronRight, CircleAlert, Clipboard, ClipboardCheck, LoaderCircle, RotateCcw, Terminal, UserRound, X } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { TimelineEntry, ToolEntry } from "../state/runtime";

export function Timeline({ entries, turnRunning, onRetry }: { entries: TimelineEntry[]; turnRunning: boolean; onRetry: () => Promise<void> }) {
  const viewport = useRef<HTMLDivElement>(null);
  const shouldFollow = useRef(true);
  const [showLatest, setShowLatest] = useState(false);
  const latestAssistant = latestAssistantId(entries);

  const scrollToLatest = () => {
    shouldFollow.current = true;
    setShowLatest(false);
    viewport.current?.scrollTo({ top: viewport.current.scrollHeight, behavior: "smooth" });
  };

  useEffect(() => {
    if (shouldFollow.current) viewport.current?.scrollTo({ top: viewport.current.scrollHeight });
  }, [entries]);

  return (
    <div
      className="timeline"
      ref={viewport}
      onScroll={(event) => {
        const element = event.currentTarget;
        const following = element.scrollHeight - element.scrollTop - element.clientHeight < 120;
        shouldFollow.current = following;
        setShowLatest(!following);
      }}
    >
      <div className="timeline-inner">
        {entries.map((entry) => {
          if (entry.kind === "message") {
            return <MessageBlock key={entry.id} entry={entry} canRetry={!turnRunning && entry.role === "assistant" && entry.id === latestAssistant} onRetry={onRetry} />;
          }
          if (entry.kind === "tool") return <ToolBlock key={entry.id} tool={entry} />;
          return (
            <div key={entry.id} className={`notice-row ${entry.level}`}>
              {entry.level === "error" ? <CircleAlert size={14} /> : <Terminal size={14} />}
              <span>{entry.content}</span>
            </div>
          );
        })}
        {turnRunning && !entries.some((entry) => entry.kind === "message" && entry.streaming) && (
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
}

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
            <img key={`${image.label}-${index}`} src={image.src} alt={image.label} loading="lazy" />
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
  return (
    <details className={`tool-block status-${tool.status}`}>
      <summary>
        <span className="tool-status-icon">
          {running ? <LoaderCircle size={14} className="spin" /> : tool.status === "success" ? <Check size={14} /> : <X size={14} />}
        </span>
        <span className="tool-name">{tool.name}</span>
        <span className="tool-summary">{toolSummary(tool)}</span>
        <ChevronRight size={13} className="summary-chevron" />
      </summary>
      <div className="tool-details">
        <Detail label="Arguments" value={tool.args} />
        {tool.result !== null && tool.result !== "" && <Detail label="Result" value={tool.result} />}
        {tool.data !== null && <Detail label="Data" value={tool.data} />}
        {tool.error && <Detail label="Error" value={tool.error} />}
        {tool.images.length > 0 && <Detail label="Images" value={tool.images} />}
      </div>
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
  const candidate = args.path || args.command || args.query || args.objective;
  return typeof candidate === "string" ? candidate : tool.status;
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
