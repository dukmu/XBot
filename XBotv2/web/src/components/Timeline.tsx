import { useEffect, useRef } from "react";
import { Brain, Check, ChevronRight, CircleAlert, LoaderCircle, Terminal, UserRound, X } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { TimelineEntry, ToolEntry } from "../state/runtime";

export function Timeline({ entries, turnRunning }: { entries: TimelineEntry[]; turnRunning: boolean }) {
  const viewport = useRef<HTMLDivElement>(null);
  const shouldFollow = useRef(true);

  useEffect(() => {
    if (shouldFollow.current) viewport.current?.scrollTo({ top: viewport.current.scrollHeight });
  }, [entries]);

  return (
    <div
      className="timeline"
      ref={viewport}
      onScroll={(event) => {
        const element = event.currentTarget;
        shouldFollow.current = element.scrollHeight - element.scrollTop - element.clientHeight < 120;
      }}
    >
      <div className="timeline-inner">
        {entries.map((entry) => {
          if (entry.kind === "message") {
            return (
              <article key={entry.id} className={`message-block ${entry.role}`}>
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
                  <div className="markdown-body">
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>{entry.content}</ReactMarkdown>
                  </div>
                )}
                {entry.streaming && !entry.content && !entry.reasoning && (
                  <div className="assistant-pending"><i /><i /><i /></div>
                )}
              </article>
            );
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
    </div>
  );
}

function ToolBlock({ tool }: { tool: ToolEntry }) {
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
      </div>
    </details>
  );
}

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
