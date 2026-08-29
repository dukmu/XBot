import { Bot, ChevronRight, Circle, GitBranch, GitFork, MoreHorizontal, PanelLeftClose, Plus, RefreshCw, Search, TerminalSquare, Trash2, X } from "lucide-react";
import { useMemo, useState } from "react";
import type { OpenSessionResponse, SessionSummary, ThreadSummary } from "../api/types";

interface SessionSidebarProps {
  open: boolean;
  sessions: SessionSummary[];
  threads: ThreadSummary[];
  current: OpenSessionResponse | null;
  onClose: () => void;
  onNew: () => void;
  onRefresh: () => Promise<void>;
  refreshing: boolean;
  onSession: (id: string) => void;
  onThread: (thread: ThreadSummary) => void;
  onFork: (id: string) => void;
  onDelete: (session: SessionSummary) => void;
}

export function SessionSidebar(props: SessionSidebarProps) {
  const [query, setQuery] = useState("");
  const [menuSession, setMenuSession] = useState("");
  const visibleSessions = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return props.sessions;
    return props.sessions.filter((session) => (
      [session.session_id, session.title, session.workspace_root]
        .some((value) => String(value || "").toLowerCase().includes(needle))
    ));
  }, [props.sessions, query]);

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
      <div className="sidebar-tools">
        <div className="session-search">
          <Search size={14} />
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search sessions"
            aria-label="Search sessions"
          />
          {query && (
            <button className="icon-button small" type="button" title="Clear search" aria-label="Clear search" onClick={() => setQuery("")}>
              <X size={13} />
            </button>
          )}
        </div>
      </div>
      <div className="sidebar-section-label">
        <span>Sessions <b>{visibleSessions.length}</b></span>
        <button
          className="icon-button small"
          type="button"
          title="Refresh sessions"
          aria-label="Refresh sessions"
          disabled={props.refreshing}
          onClick={() => void props.onRefresh()}
        >
          <RefreshCw size={13} className={props.refreshing ? "spin" : ""} />
        </button>
      </div>
      <nav className="session-list" aria-label="Sessions">
        {visibleSessions.map((session) => {
          const active = session.session_id === props.current?.session_id;
          return (
            <div key={session.session_id} className="session-group" onBlur={(event) => {
              if (!event.currentTarget.contains(event.relatedTarget)) setMenuSession("");
            }}>
              <div className="session-line">
                <button
                  className={`session-row ${active ? "selected" : ""}`}
                  onClick={() => props.onSession(session.session_id)}
                  title={session.session_id}
                >
                  <Circle size={7} fill={session.status === "active" ? "currentColor" : "none"} />
                  <span className="session-label">
                    <b>{session.title || shortId(session.session_id)}</b>
                    {session.workspace_root && <small>{session.workspace_root}</small>}
                  </span>
                  <small>{session.thread_count}</small>
                  <ChevronRight size={13} />
                </button>
                <div className="session-actions">
                  <button
                    type="button"
                    className="icon-button small session-more"
                    aria-label={`More actions for ${session.title || session.session_id}`}
                    aria-expanded={menuSession === session.session_id}
                    onClick={() => setMenuSession((value) => value === session.session_id ? "" : session.session_id)}
                  >
                    <MoreHorizontal size={14} />
                  </button>
                </div>
              </div>
              {menuSession === session.session_id && (
                <div className="session-action-menu" role="menu">
                  <button type="button" role="menuitem" onClick={() => {
                    setMenuSession("");
                    props.onFork(session.session_id);
                  }}><GitFork size={13} /> Fork</button>
                  <button type="button" role="menuitem" className="danger" onClick={() => {
                    setMenuSession("");
                    props.onDelete(session);
                  }}><Trash2 size={13} /> Delete</button>
                </div>
              )}
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
        {!visibleSessions.length && <div className="sidebar-empty">{query ? "No matching sessions" : "No sessions"}</div>}
      </nav>
    </aside>
  );
}

function shortId(value: string): string {
  if (value.length <= 21) return value;
  return `${value.slice(0, 10)}...${value.slice(-7)}`;
}
