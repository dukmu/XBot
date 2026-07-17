import { useEffect, useState } from "react";
import { AlertCircle, Menu, Plus, RefreshCw, TerminalSquare, X } from "lucide-react";
import { useXBot } from "../state/useXBot";
import { Composer } from "../components/Composer";
import { InteractionDialog } from "../components/InteractionDialog";
import { RuntimeHeader } from "../components/RuntimeHeader";
import { SessionSidebar } from "../components/SessionSidebar";
import { StatusBar } from "../components/StatusBar";
import { TaskDock } from "../components/TaskDock";
import { Timeline } from "../components/Timeline";

export function App() {
  const runtime = useXBot();
  const { state } = runtime;
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [newSessionOpen, setNewSessionOpen] = useState(false);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && state.turnRunning && !state.interactions.length) {
        void runtime.interrupt();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [runtime, state.interactions.length, state.turnRunning]);

  return (
    <div className="app-shell">
      <SessionSidebar
        open={sidebarOpen}
        sessions={state.sessions}
        threads={state.threads}
        current={state.current}
        onClose={() => setSidebarOpen(false)}
        onNew={() => setNewSessionOpen(true)}
        onSession={(id) => {
          setSidebarOpen(false);
          void runtime.resumeSession(id);
        }}
        onThread={(thread) => {
          setSidebarOpen(false);
          void runtime.selectThread(thread);
        }}
      />
      {sidebarOpen && <button className="sidebar-scrim" aria-label="Close sidebar" onClick={() => setSidebarOpen(false)} />}

      <main className="workbench">
        <RuntimeHeader
          state={state}
          onMenu={() => setSidebarOpen(true)}
          onAgent={runtime.selectAgent}
          onProvider={runtime.selectProvider}
          onUndo={runtime.undo}
          onFork={runtime.fork}
          onClear={runtime.clear}
        />

        {state.error && (
          <div className="error-banner" role="alert">
            <AlertCircle size={16} />
            <span>{state.error}</span>
            {!state.connected && state.current && (
              <button className="text-button" onClick={() => void runtime.resumeSession()}>
                <RefreshCw size={14} /> Reconnect
              </button>
            )}
            <button className="icon-button small" title="Dismiss" aria-label="Dismiss error" onClick={runtime.clearError}>
              <X size={14} />
            </button>
          </div>
        )}

        {state.current ? (
          <>
            <Timeline entries={state.entries} turnRunning={state.turnRunning} />
            <div className="runtime-controls">
              <TaskDock
                tasks={Object.values(state.tasks)}
                onStop={runtime.stopTask}
                onStopAll={runtime.stopAllTasks}
              />
              <Composer
                running={state.turnRunning}
                queued={state.queuedMessages}
                onSend={runtime.sendMessage}
                onInterrupt={runtime.interrupt}
              />
            </div>
          </>
        ) : (
          <section className="empty-workbench">
            <TerminalSquare size={42} strokeWidth={1.5} />
            <h1>XBot</h1>
            <p>No session selected</p>
            <button className="primary-button" onClick={() => setNewSessionOpen(true)}>
              <Plus size={16} /> New session
            </button>
            <button className="mobile-session-button" onClick={() => setSidebarOpen(true)}>
              <Menu size={16} /> Sessions
            </button>
          </section>
        )}

        <StatusBar state={state} />
      </main>

      {state.interactions[0] && state.current && (
        <InteractionDialog
          request={state.interactions[0]}
          pendingCount={state.interactions.length}
          onResolve={runtime.resolveInteraction}
        />
      )}

      {newSessionOpen && (
        <NewSessionDialog
          onClose={() => setNewSessionOpen(false)}
          onCreate={(workspace) => {
            setNewSessionOpen(false);
            void runtime.createSession(workspace);
          }}
        />
      )}

      {state.loading && <div className="loading-line" aria-label="Loading" />}
    </div>
  );
}

function NewSessionDialog({
  onClose,
  onCreate,
}: {
  onClose: () => void;
  onCreate: (workspace: string) => void;
}) {
  const [workspace, setWorkspace] = useState("");
  return (
    <div className="dialog-backdrop" role="presentation" onMouseDown={(event) => {
      if (event.currentTarget === event.target) onClose();
    }}>
      <form className="dialog new-session-dialog" onSubmit={(event) => {
        event.preventDefault();
        onCreate(workspace);
      }}>
        <div className="dialog-heading">
          <div>
            <span className="eyebrow">Session</span>
            <h2>New workspace</h2>
          </div>
          <button type="button" className="icon-button" title="Close" aria-label="Close" onClick={onClose}>
            <X size={17} />
          </button>
        </div>
        <label className="field-label" htmlFor="workspace-root">Workspace path</label>
        <input
          id="workspace-root"
          autoFocus
          value={workspace}
          onChange={(event) => setWorkspace(event.target.value)}
          placeholder="Server default"
        />
        <div className="dialog-actions">
          <button type="button" className="secondary-button" onClick={onClose}>Cancel</button>
          <button type="submit" className="primary-button"><Plus size={16} /> Create</button>
        </div>
      </form>
    </div>
  );
}
