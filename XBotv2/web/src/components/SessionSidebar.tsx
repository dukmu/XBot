import { ArrowDown, ArrowUp, Bot, Check, ChevronDown, ChevronRight, Circle, Folder, GitBranch, GitFork, MoreHorizontal, PanelLeftClose, PanelLeftOpen, Pencil, Plus, RefreshCw, Search, Settings, TerminalSquare, Trash2, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import type { OpenSessionResponse, SessionSummary, ThreadSummary, WorkspaceData } from "../api/types";

interface SessionSidebarProps {
  open: boolean;
  collapsed: boolean;
  width: number;
  sessions: readonly SessionSummary[];
  workspaces: readonly WorkspaceData[];
  archivedSessionIds: readonly string[];
  threads: ThreadSummary[];
  current: Pick<OpenSessionResponse, "session_id" | "thread_id"> | null;
  onClose: () => void;
  onToggle: () => void;
  onSettings: () => void;
  onNew: () => void;
  onRefresh: () => Promise<void>;
  refreshing: boolean;
  onSession: (id: string) => void;
  onThread: (thread: ThreadSummary) => void;
  onFork: (id: string) => void;
  onDelete: (session: SessionSummary) => void;
  onRenameSession: (sessionId: string, title: string) => void;
  onArchiveSession: (sessionId: string, archived: boolean) => void;
  onRenameWorkspace: (workspaceId: string, title: string) => void;
  onDeleteWorkspace: (workspaceId: string) => void;
  onMoveWorkspace: (workspaceId: string, direction: -1 | 1) => void;
  onMoveSession: (workspaceId: string, sessionId: string, direction: -1 | 1) => void;
}

const COLLAPSED_SESSION_LIMIT = 5;

export function SessionSidebar(props: SessionSidebarProps) {
  const [wideMounted, setWideMounted] = useState(!props.collapsed);
  const [query, setQuery] = useState("");
  const [collapsedWorkspaces, setCollapsedWorkspaces] = useState<Set<string>>(new Set());
  const [expandedSessionGroups, setExpandedSessionGroups] = useState<Set<string>>(new Set());
  const [editingWorkspace, setEditingWorkspace] = useState("");
  const [workspaceTitle, setWorkspaceTitle] = useState("");
  const [confirmWorkspaceDelete, setConfirmWorkspaceDelete] = useState("");
  const [menuWorkspace, setMenuWorkspace] = useState("");
  const searchInput = useRef<HTMLInputElement>(null);
  const lastWideWidth = useRef(props.width);
  if (!props.collapsed) lastWideWidth.current = props.width;
  useEffect(() => {
    if (!props.collapsed) {
      setWideMounted(true);
      return;
    }
    const timer = window.setTimeout(() => setWideMounted(false), 150);
    return () => window.clearTimeout(timer);
  }, [props.collapsed]);
  const wide = !props.collapsed || wideMounted;
  const rail = !wide;
  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const matches = (values: unknown[]) => !needle || values.some(
      (value) => String(value || "").toLowerCase().includes(needle),
    );
    const archivedIds = new Set(props.archivedSessionIds);
    const sessionsById = new Map(props.sessions.map((session) => [session.session_id, session]));
    const groups = props.workspaces.flatMap((workspace) => {
      const sessions = workspace.session_ids.flatMap((sessionId) => {
        const session = sessionsById.get(sessionId);
        return session && !archivedIds.has(sessionId) ? [session] : [];
      });
      const workspaceMatches = matches([workspace.title, workspace.path]);
      const filtered = workspaceMatches
        ? sessions
        : sessions.filter((session) => matches([session.session_id, session.title, session.workspace_root]));
      return workspaceMatches || filtered.length ? [{ workspace, sessions: filtered }] : [];
    });
    const registered = new Set(props.workspaces.flatMap((workspace) => workspace.session_ids));
    const ungrouped = props.sessions.filter((session) => (
      !registered.has(session.session_id)
      && !archivedIds.has(session.session_id)
      && matches([session.session_id, session.title, session.workspace_root])
    ));
    const archived = props.sessions.filter((session) => (
      archivedIds.has(session.session_id)
      && matches([session.session_id, session.title, session.workspace_root])
    ));
    return { groups, ungrouped, archived };
  }, [props.archivedSessionIds, props.sessions, props.workspaces, query]);
  const visibleCount = visible.groups.reduce((count, group) => count + group.sessions.length, 0)
    + visible.ungrouped.length + visible.archived.length;

  const toggleWorkspace = (workspaceId: string) => {
    setCollapsedWorkspaces((current) => {
      const next = new Set(current);
      if (next.has(workspaceId)) next.delete(workspaceId);
      else next.add(workspaceId);
      return next;
    });
  };

  return (
    <aside
      className={`session-sidebar ${props.open ? "open" : ""} ${rail ? "collapsed" : ""} ${props.collapsed && wide ? "fading" : ""}`}
      style={{ width: wide && props.collapsed ? lastWideWidth.current : props.width }}
    >
      <div className="brand-row">
        <span className="brand-mark"><TerminalSquare size={19} /></span>
        <strong className="sidebar-wide">XBot</strong>
        <button className="icon-button settings-button sidebar-wide" title="Settings" aria-label="Open settings" onClick={props.onSettings}>
          <Settings size={16} />
        </button>
        <button className="icon-button sidebar-toggle" title={props.collapsed ? "Expand sidebar" : "Collapse sidebar"} aria-label={props.collapsed ? "Expand sidebar" : "Collapse sidebar"} onClick={props.onToggle}>
          {rail ? <PanelLeftOpen size={18} /> : <PanelLeftClose size={17} />}
        </button>
        <button className="icon-button sidebar-close" title="Close sidebar" aria-label="Close sidebar" onClick={props.onClose}>
          <PanelLeftClose size={17} />
        </button>
      </div>
      <button className="new-session-button" title="New session" aria-label="New session" onClick={props.onNew}>
        <Plus size={rail ? 18 : 16} /> <span className="sidebar-wide">New session</span>
      </button>
      {rail && (
        <div className="sidebar-rail-actions">
          <button type="button" title="Search sessions" aria-label="Search sessions" onClick={() => {
            props.onToggle();
            window.setTimeout(() => searchInput.current?.focus(), 310);
          }}><Search size={18} /></button>
          <button type="button" title="Refresh sessions" aria-label="Refresh sessions" disabled={props.refreshing} onClick={() => void props.onRefresh()}>
            <RefreshCw size={18} className={props.refreshing ? "spin" : ""} />
          </button>
          <button type="button" title="Settings" aria-label="Open settings" onClick={props.onSettings}>
            <Settings size={18} />
          </button>
        </div>
      )}
      <div className="sidebar-tools sidebar-wide">
        <div className="session-search">
          <Search size={14} />
          <input
            ref={searchInput}
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
      <div className="sidebar-section-label sidebar-wide">
        <span>Sessions <b>{visibleCount}</b></span>
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
      <nav className="session-list sidebar-wide" aria-label="Sessions">
        {visible.groups.map(({ workspace, sessions }, workspaceIndex) => {
          const collapsed = collapsedWorkspaces.has(workspace.workspace_id) && !query;
          const sessionRows = query || expandedSessionGroups.has(workspace.workspace_id)
            ? sessions
            : sessions.slice(0, COLLAPSED_SESSION_LIMIT);
          const hiddenSessionCount = sessions.length - sessionRows.length;
          return (
            <section className="workspace-group" key={workspace.workspace_id} onBlur={(event) => {
              if (!event.currentTarget.contains(event.relatedTarget)) setMenuWorkspace("");
            }}>
              <div className="workspace-row">
                <button className="workspace-toggle" type="button" onClick={() => toggleWorkspace(workspace.workspace_id)} title={workspace.path}>
                  {collapsed ? <ChevronRight size={13} /> : <ChevronDown size={13} />}
                  <Folder size={14} />
                  <span><b>{workspace.title}</b><small>{workspace.path}</small></span>
                  <small>{workspace.session_ids.length}</small>
                </button>
                <button className="icon-button small" type="button" aria-label={`More actions for workspace ${workspace.title}`} aria-expanded={menuWorkspace === workspace.workspace_id} onClick={() => setMenuWorkspace(menuWorkspace === workspace.workspace_id ? "" : workspace.workspace_id)}><MoreHorizontal size={14} /></button>
              </div>
              {menuWorkspace === workspace.workspace_id && (
                <div className="session-action-menu workspace-action-menu" role="menu">
                  <button type="button" role="menuitem" disabled={workspaceIndex === 0} onClick={() => {
                    setMenuWorkspace("");
                    props.onMoveWorkspace(workspace.workspace_id, -1);
                  }}><ArrowUp size={13} /> Move up</button>
                  <button type="button" role="menuitem" disabled={workspaceIndex === visible.groups.length - 1} onClick={() => {
                    setMenuWorkspace("");
                    props.onMoveWorkspace(workspace.workspace_id, 1);
                  }}><ArrowDown size={13} /> Move down</button>
                  <button type="button" role="menuitem" onClick={() => {
                    setMenuWorkspace("");
                    setEditingWorkspace(workspace.workspace_id);
                    setWorkspaceTitle(workspace.title);
                    setConfirmWorkspaceDelete("");
                  }}><Pencil size={13} /> Rename</button>
                  <button type="button" role="menuitem" className="danger" onClick={() => {
                    setMenuWorkspace("");
                    setConfirmWorkspaceDelete(workspace.workspace_id);
                    setEditingWorkspace("");
                  }}><Trash2 size={13} /> Remove</button>
                </div>
              )}
              {editingWorkspace === workspace.workspace_id && (
                <form className="workspace-inline-action" onSubmit={(event) => {
                  event.preventDefault();
                  const title = workspaceTitle.trim();
                  if (!title) return;
                  props.onRenameWorkspace(workspace.workspace_id, title);
                  setEditingWorkspace("");
                }}>
                  <input autoFocus value={workspaceTitle} aria-label="Workspace title" onChange={(event) => setWorkspaceTitle(event.target.value)} />
                  <button className="icon-button small" type="submit" aria-label="Save workspace title"><Check size={13} /></button>
                  <button className="icon-button small" type="button" aria-label="Cancel rename" onClick={() => setEditingWorkspace("")}><X size={13} /></button>
                </form>
              )}
              {confirmWorkspaceDelete === workspace.workspace_id && (
                <div className="workspace-inline-action workspace-remove-confirm" role="alert">
                  <span>Remove workspace from the sidebar? Sessions are kept.</span>
                  <button type="button" className="text-button danger" onClick={() => {
                    props.onDeleteWorkspace(workspace.workspace_id);
                    setConfirmWorkspaceDelete("");
                  }}>Remove</button>
                  <button type="button" className="text-button" onClick={() => setConfirmWorkspaceDelete("")}>Cancel</button>
                </div>
              )}
              {!collapsed && sessionRows.map((session, sessionIndex) => (
                <SessionItem
                  key={session.session_id}
                  session={session}
                  sidebar={props}
                  archived={false}
                  workspace={workspace}
                  sessionIndex={sessionIndex}
                />
              ))}
              {!collapsed && hiddenSessionCount > 0 && (
                <button
                  type="button"
                  className="workspace-show-more"
                  onClick={() => setExpandedSessionGroups((current) => new Set(current).add(workspace.workspace_id))}
                >Show {hiddenSessionCount} more</button>
              )}
              {!collapsed && sessions.length > COLLAPSED_SESSION_LIMIT && hiddenSessionCount === 0 && !query && (
                <button
                  type="button"
                  className="workspace-show-more"
                  onClick={() => setExpandedSessionGroups((current) => {
                    const next = new Set(current);
                    next.delete(workspace.workspace_id);
                    return next;
                  })}
                >Show less</button>
              )}
              {!collapsed && !sessions.length && <div className="workspace-empty">No matching sessions</div>}
            </section>
          );
        })}
        {visible.ungrouped.length > 0 && props.workspaces.length > 0 && <div className="ungrouped-label">Other sessions</div>}
        {visible.ungrouped.map((session) => <SessionItem key={session.session_id} session={session} sidebar={props} archived={false} />)}
        {visible.archived.length > 0 && (
          <details className="archived-sessions" open={Boolean(query)}>
            <summary><ChevronRight size={13} /> Archived <small>{visible.archived.length}</small></summary>
            {visible.archived.map((session) => <SessionItem key={session.session_id} session={session} sidebar={props} archived />)}
          </details>
        )}
        {!visibleCount && !visible.groups.length && <div className="sidebar-empty">{query ? "No matching sessions" : "No sessions"}</div>}
      </nav>
    </aside>
  );
}

function SessionItem({
  session,
  sidebar,
  archived,
  workspace,
  sessionIndex = -1,
}: {
  session: SessionSummary;
  sidebar: SessionSidebarProps;
  archived: boolean;
  workspace?: WorkspaceData;
  sessionIndex?: number;
}) {
  const [menuOpen, setMenuOpen] = useState(false);
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(session.title || session.session_id);
  const active = session.session_id === sidebar.current?.session_id;
  return (
            <div className="session-group" onBlur={(event) => {
              if (!event.currentTarget.contains(event.relatedTarget)) setMenuOpen(false);
            }}>
              <div className="session-line">
                <button
                  className={`session-row ${active ? "selected" : ""}`}
                  onClick={() => sidebar.onSession(session.session_id)}
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
                    aria-expanded={menuOpen}
                    onClick={() => setMenuOpen((open) => !open)}
                  >
                    <MoreHorizontal size={14} />
                  </button>
                </div>
              </div>
              {menuOpen && (
                <div className="session-action-menu" role="menu">
                  {workspace && (
                    <>
                      <button type="button" role="menuitem" disabled={sessionIndex === 0} onClick={() => {
                        setMenuOpen(false);
                        sidebar.onMoveSession(workspace.workspace_id, session.session_id, -1);
                      }}><ArrowUp size={13} /> Move up</button>
                      <button type="button" role="menuitem" disabled={sessionIndex === workspace.session_ids.length - 1} onClick={() => {
                        setMenuOpen(false);
                        sidebar.onMoveSession(workspace.workspace_id, session.session_id, 1);
                      }}><ArrowDown size={13} /> Move down</button>
                    </>
                  )}
                  <button type="button" role="menuitem" onClick={() => {
                    setMenuOpen(false);
                    setTitle(session.title || session.session_id);
                    setEditing(true);
                  }}><Pencil size={13} /> Rename</button>
                  <button type="button" role="menuitem" onClick={() => {
                    setMenuOpen(false);
                    sidebar.onArchiveSession(session.session_id, !archived);
                  }}>{archived ? <><ArrowUp size={13} /> Restore</> : <><ArrowDown size={13} /> Archive</>}</button>
                  <button type="button" role="menuitem" onClick={() => {
                    setMenuOpen(false);
                    sidebar.onFork(session.session_id);
                  }}><GitFork size={13} /> Fork</button>
                  <button type="button" role="menuitem" className="danger" onClick={() => {
                    setMenuOpen(false);
                    sidebar.onDelete(session);
                  }}><Trash2 size={13} /> Delete</button>
                </div>
              )}
              {editing && (
                <form className="workspace-inline-action session-rename-action" onSubmit={(event) => {
                  event.preventDefault();
                  const value = title.trim();
                  if (!value) return;
                  sidebar.onRenameSession(session.session_id, value);
                  setEditing(false);
                }}>
                  <input autoFocus value={title} aria-label="Session title" onChange={(event) => setTitle(event.target.value)} />
                  <button className="icon-button small" type="submit" aria-label="Save session title"><Check size={13} /></button>
                  <button className="icon-button small" type="button" aria-label="Cancel session rename" onClick={() => setEditing(false)}><X size={13} /></button>
                </form>
              )}
              {active && sidebar.threads.length > 0 && (
                <div className="thread-list">
                  {sidebar.threads.map((thread) => (
                    <button
                      key={thread.thread_id}
                      className={`thread-row ${thread.thread_id === sidebar.current?.thread_id ? "selected" : ""}`}
                      onClick={() => sidebar.onThread(thread)}
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
}

function shortId(value: string): string {
  if (value.length <= 21) return value;
  return `${value.slice(0, 10)}...${value.slice(-7)}`;
}
