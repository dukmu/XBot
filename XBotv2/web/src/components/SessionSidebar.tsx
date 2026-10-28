import {
  ArrowDown, ArrowUp, Bot, Check, ChevronDown, ChevronRight, Circle, Folder,
  GitBranch, GitFork, MoreHorizontal, PanelLeftClose, PanelLeftOpen, Pencil,
  Plus, RefreshCw, Search, Settings, TerminalSquare, Trash2, X,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { SessionView, SessionSummary, ThreadSummary, WorkspaceData } from "../api/types";
import { ThreadPageControls } from "./ThreadPageControls";

interface SessionSidebarProps {
  open: boolean;
  collapsed: boolean;
  width: number;
  catalog: import("../client/SessionCatalog").SessionCatalogSnapshot;
  workspacePage: import("../client/WorkspaceManager").WorkspaceSnapshot;
  threads: ThreadSummary[];
  threadPreviousCursor: string | null;
  threadNextCursor: string | null;
  threadPageLoading: boolean;
  onPageThreads: (direction: "previous" | "next" | "refresh") => void;
  current: Pick<SessionView, "session_id" | "thread_id"> | null;
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
  onSelectCatalog: (scope: import("../client/SessionCatalog").SessionScope) => void;
  onNextSessions: () => void;
  onPreviousSessions: () => void;
  onNextWorkspaces: () => void;
  onPreviousWorkspaces: () => void;
}

export function SessionSidebar(props: SessionSidebarProps) {
  const [wideMounted, setWideMounted] = useState(!props.collapsed);
  const [query, setQuery] = useState("");
  const [editingWorkspace, setEditingWorkspace] = useState("");
  const [workspaceTitle, setWorkspaceTitle] = useState("");
  const [confirmWorkspaceDelete, setConfirmWorkspaceDelete] = useState("");
  const [menuWorkspace, setMenuWorkspace] = useState("");
  const lastWideWidth = useRef(props.width);
  if (!props.collapsed) lastWideWidth.current = props.width;
  useEffect(() => {
    if (!props.collapsed) { setWideMounted(true); return; }
    const timer = window.setTimeout(() => setWideMounted(false), 150);
    return () => window.clearTimeout(timer);
  }, [props.collapsed]);
  const wide = !props.collapsed || wideMounted;
  const rail = !wide;
  const scope = props.catalog.scope;
  const needle = query.trim().toLowerCase();
  const matches = (session: SessionSummary) => !needle
    || [session.title, session.session_id, session.workspace_root].some(
      (value) => String(value || "").toLowerCase().includes(needle),
    );
  const archivedIds = new Set(props.catalog.archivedIds);
  const rows = props.catalog.items.filter(matches);
  const selectedWorkspace = scope.kind === "workspace"
    ? props.workspacePage.items.find((item) => item.workspace_id === scope.workspaceId)
    : undefined;
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
        <Plus size={rail ? 18 : 16} /><span className="sidebar-wide">New session</span>
      </button>
      <div className="sidebar-wide sidebar-content">
        <div className="session-search">
          <Search size={14} />
          <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search current page" aria-label="Search current page" />
          {query && (
            <button className="icon-button small" type="button" aria-label="Clear search" onClick={() => setQuery("")}>
              <X size={13} />
            </button>
          )}
        </div>
        <div className="sidebar-section-label">
          <span>Workspaces</span>
          <button className="icon-button small" type="button" title="Refresh sessions" aria-label="Refresh sessions" disabled={props.refreshing} onClick={() => void props.onRefresh()}>
            <RefreshCw size={13} />
          </button>
        </div>
        <nav className="session-list" aria-label="Sessions">
          {props.workspacePage.state === "loading" && !props.workspacePage.items.length && (
            <div className="sidebar-empty" role="status">Loading workspaces…</div>
          )}
          {props.workspacePage.state === "error" && (
            <div className="sidebar-empty" role="alert">
              Could not load workspaces. <button className="text-button" type="button" onClick={() => void props.onRefresh()}>Retry</button>
            </div>
          )}
          {props.workspacePage.items.map((workspace, index) => (
            <section className="workspace-group" key={workspace.workspace_id} onBlur={(event) => {
              if (!event.currentTarget.contains(event.relatedTarget)) setMenuWorkspace("");
            }}>
              <div className="workspace-row">
                <button
                  className="workspace-toggle"
                  type="button"
                  onClick={() => props.onSelectCatalog({ kind: "workspace", workspaceId: workspace.workspace_id })}
                  title={workspace.path}
                  aria-expanded={scope.kind === "workspace" && scope.workspaceId === workspace.workspace_id}
                >
                  {scope.kind === "workspace" && scope.workspaceId === workspace.workspace_id ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
                  <Folder size={14} />
                  <span><b>{workspace.title}</b><small>{workspace.path}</small></span>
                  <small>{workspace.session_count}</small>
                </button>
                <button
                  className="icon-button small"
                  type="button"
                  aria-label={`More actions for workspace ${workspace.title}`}
                  aria-expanded={menuWorkspace === workspace.workspace_id}
                  onClick={() => setMenuWorkspace(menuWorkspace === workspace.workspace_id ? "" : workspace.workspace_id)}
                ><MoreHorizontal size={14} /></button>
              </div>
              {menuWorkspace === workspace.workspace_id && (
                <div className="session-action-menu workspace-action-menu" role="menu">
                  <button type="button" role="menuitem" disabled={index === 0} onClick={() => {
                    setMenuWorkspace("");
                    props.onMoveWorkspace(workspace.workspace_id, -1);
                  }}><ArrowUp size={13} /> Move up</button>
                  <button type="button" role="menuitem" disabled={index === props.workspacePage.items.length - 1} onClick={() => {
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
            </section>
          ))}
          <div className="catalog-pager" aria-label="Workspace pages">
            <button className="text-button" type="button" disabled={!props.workspacePage.canPrevious || props.workspacePage.state === "loading"} onClick={props.onPreviousWorkspaces}>Previous workspaces</button>
            <button className="text-button" type="button" disabled={!props.workspacePage.canNext || props.workspacePage.state === "loading"} onClick={props.onNextWorkspaces}>Next workspaces</button>
          </div>
          <div className="sidebar-section-label">
            <span>{selectedWorkspace?.title || (scope.kind === "archived" ? "Archived sessions" : scope.kind === "workspace" ? "Workspace sessions" : "All sessions")}</span>
          </div>
          <div className="catalog-pager" aria-label="Session filters">
            <button className="text-button" type="button" aria-pressed={scope.kind === "all"} onClick={() => props.onSelectCatalog({ kind: "all" })}>All sessions</button>
            <button className="text-button" type="button" aria-pressed={scope.kind === "archived"} onClick={() => props.onSelectCatalog({ kind: "archived" })}>Archived</button>
          </div>
          {rows.map((session) => (
            <SessionItem
              key={session.session_id}
              session={session}
              sidebar={props}
              archived={scope.kind === "all" ? undefined : archivedIds.has(session.session_id)}
              workspace={selectedWorkspace}
              sessionIndex={props.catalog.items.findIndex((item) => item.session_id === session.session_id)}
            />
          ))}
          {props.catalog.state === "loading" && !rows.length && (
            <div className="sidebar-empty" role="status">Loading sessions…</div>
          )}
          {props.catalog.state === "error" && (
            <div className="sidebar-empty" role="alert">
              Could not load sessions. <button className="text-button" type="button" onClick={() => void props.onRefresh()}>Retry</button>
            </div>
          )}
          {!rows.length && props.catalog.state === "idle" && (
            <div className="sidebar-empty">{needle ? "No matches on this page" : "No sessions on this page"}</div>
          )}
          <div className="catalog-pager" aria-label="Session pages">
            <button className="text-button" type="button" disabled={!props.catalog.canPrevious || props.catalog.state === "loading"} onClick={props.onPreviousSessions}>Previous sessions</button>
            <button className="text-button" type="button" disabled={!props.catalog.canNext || props.catalog.state === "loading"} onClick={props.onNextSessions}>Next sessions</button>
          </div>
        </nav>
      </div>
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
  archived: boolean | undefined;
  workspace?: WorkspaceData;
  sessionIndex?: number;
}) {
  const [menuOpen, setMenuOpen] = useState(false);
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(session.title);
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
                    <b>{session.title}</b>
                    {session.workspace_root && <small>{session.workspace_root}</small>}
                  </span>
                  <small>{session.thread_count}</small>
                  <ChevronRight size={13} />
                </button>
                <div className="session-actions">
                  <button
                    type="button"
                    className="icon-button small session-more"
                    aria-label={`More actions for ${session.title}`}
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
                      <button type="button" role="menuitem" disabled={sessionIndex === sidebar.catalog.items.length - 1} onClick={() => {
                        setMenuOpen(false);
                        sidebar.onMoveSession(workspace.workspace_id, session.session_id, 1);
                      }}><ArrowDown size={13} /> Move down</button>
                    </>
                  )}
                  <button type="button" role="menuitem" onClick={() => {
                    setMenuOpen(false);
                    setTitle(session.title);
                    setEditing(true);
                  }}><Pencil size={13} /> Rename</button>
                  {archived !== true && (
                    <button type="button" role="menuitem" onClick={() => {
                      setMenuOpen(false);
                      sidebar.onArchiveSession(session.session_id, true);
                    }}><ArrowDown size={13} /> Archive</button>
                  )}
                  {archived !== false && (
                    <button type="button" role="menuitem" onClick={() => {
                      setMenuOpen(false);
                      sidebar.onArchiveSession(session.session_id, false);
                    }}><ArrowUp size={13} /> Restore</button>
                  )}
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
              {active && (
                <div className="thread-list">
                  {sidebar.threads.map((thread) => (
                    <button
                      key={thread.thread_id}
                      className={`thread-row ${thread.thread_id === sidebar.current?.thread_id ? "selected" : ""}`}
                      onClick={() => sidebar.onThread(thread)}
                      title={thread.thread_id}
                    >
                      {thread.kind === "subagent" ? <GitBranch size={13} /> : <Bot size={13} />}
                      <span className="thread-copy">
                        <b>{thread.kind === "subagent" ? (thread.agent || "subagent") : (thread.agent || "agent")}</b>
                        {thread.kind === "subagent" && <small>{shortId(thread.thread_id)}</small>}
                      </span>
                      {thread.turn_status === "running" && <i className="activity-dot" />}
                    </button>
                  ))}
                  <ThreadPageControls
                    previousCursor={sidebar.threadPreviousCursor}
                    nextCursor={sidebar.threadNextCursor}
                    loading={sidebar.threadPageLoading}
                    onPage={sidebar.onPageThreads}
                  />
                </div>
              )}
            </div>
  );
}

function shortId(value: string): string {
  if (value.length <= 21) return value;
  return `${value.slice(0, 10)}...${value.slice(-7)}`;
}
