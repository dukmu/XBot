import { Activity, Bot, GitBranch } from "lucide-react";
import type { ThreadSummary } from "../api/types";

export function ThreadActivityPanel({
  threads,
  currentThreadId,
  onSelect,
}: {
  threads: readonly ThreadSummary[];
  currentThreadId: string;
  onSelect: (thread: ThreadSummary) => void;
}) {
  if (threads.length < 2) return null;
  return (
    <section className="thread-activity-panel" aria-label="Agent activity">
      <header className="thread-activity-header">
        <Activity size={14} />
        <strong>Agent activity</strong>
        <small>{threads.filter((thread) => thread.turn_status === "running").length} running</small>
      </header>
      <div className="thread-activity-list">
        {threads.map((thread) => {
          const selected = thread.thread_id === currentThreadId;
          const running = thread.turn_status === "running";
          return (
            <button
              className={`thread-activity-card ${selected ? "selected" : ""}`}
              key={thread.thread_id}
              type="button"
              aria-current={selected ? "page" : undefined}
              onClick={() => onSelect(thread)}
            >
              <span className={`thread-activity-icon ${running ? "running" : ""}`}>
                {thread.kind === "subagent" ? <GitBranch size={13} /> : <Bot size={13} />}
              </span>
              <span className="thread-activity-copy">
                <b>{thread.kind === "subagent" ? (thread.agent || "subagent") : (thread.agent || "agent")}</b>
                <small>{thread.model || thread.thread_id} · {thread.message_count} messages</small>
              </span>
              <span className={`thread-activity-status ${running ? "running" : ""}`}>
                <b>{running ? "Running" : "Idle"}</b>
                <small>{thread.usage.total_tokens.toLocaleString()} tok</small>
              </span>
            </button>
          );
        })}
      </div>
    </section>
  );
}
