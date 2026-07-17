import { Bot, ChevronRight, Circle, GitBranch, PanelLeftClose, Plus, TerminalSquare } from "lucide-react";
import type { OpenSessionResponse, SessionSummary, ThreadSummary } from "../api/types";

interface SessionSidebarProps {
  open: boolean;
  sessions: SessionSummary[];
  threads: ThreadSummary[];
  current: OpenSessionResponse | null;
  onClose: () => void;
  onNew: () => void;
  onSession: (id: string) => void;
  onThread: (thread: ThreadSummary) => void;
}

export function SessionSidebar(props: SessionSidebarProps) {
  return (
    <aside className={`session-sidebar ${props.open ? "open" : ""}`}>
      <div className="brand-row">
        <span className="brand-mark"><TerminalSquare size={19} /></span>
        <strong>XBot</strong>
        <button className="icon-button sidebar-close" title="Close sidebar" aria-label="Close sidebar" onClick={props.onClose}>
          <PanelLeftClose size={17} />
        </button>
      </div>
      <button className="new-session-button" onClick={props.onNew}>
        <Plus size={16} /> New session
      </button>
      <div className="sidebar-section-label">Sessions</div>
      <nav className="session-list" aria-label="Sessions">
        {props.sessions.map((session) => {
          const active = session.session_id === props.current?.session_id;
          return (
            <div key={session.session_id} className="session-group">
              <button
                className={`session-row ${active ? "selected" : ""}`}
                onClick={() => props.onSession(session.session_id)}
                title={session.session_id}
              >
                <Circle size={7} fill={session.status === "active" ? "currentColor" : "none"} />
                <span>{shortId(session.session_id)}</span>
                <small>{session.thread_count}</small>
                <ChevronRight size={13} />
              </button>
              {active && props.threads.length > 0 && (
                <div className="thread-list">
                  {props.threads.map((thread) => (
                    <button
                      key={thread.thread_id}
                      className={`thread-row ${thread.thread_id === props.current?.thread_id ? "selected" : ""}`}
                      onClick={() => props.onThread(thread)}
                      title={thread.thread_id}
                    >
                      {thread.kind === "subagent" ? <GitBranch size={13} /> : <Bot size={13} />}
                      <span>{thread.thread_id === "agent" ? (thread.agent || "agent") : shortId(thread.thread_id)}</span>
                      {thread.turn_status === "running" && <i className="activity-dot" />}
                    </button>
                  ))}
                </div>
              )}
            </div>
          );
        })}
        {!props.sessions.length && <div className="sidebar-empty">No sessions</div>}
      </nav>
    </aside>
  );
}

function shortId(value: string): string {
  if (value.length <= 21) return value;
  return `${value.slice(0, 10)}...${value.slice(-7)}`;
}
