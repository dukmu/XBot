import { useEffect, useMemo, useState } from "react";
import { AlertCircle, LoaderCircle, Menu, Plus, RefreshCw, TerminalSquare, Trash2, X } from "lucide-react";
import { useXBot } from "../state/useXBot";
import { Composer, type PendingAttachment } from "../components/Composer";
import { CommandHelpDialog } from "../components/CommandHelpDialog";
import { CommandOutput } from "../components/CommandOutput";
import { InteractionDialog } from "../components/InteractionDialog";
import { RuntimeHeader } from "../components/RuntimeHeader";
import { SessionSidebar } from "../components/SessionSidebar";
import { StatusBar } from "../components/StatusBar";
import { TaskDock } from "../components/TaskDock";
import { TodoDock } from "../components/TodoDock";
import { Timeline } from "../components/Timeline";
import { commandCatalog, parseCommand } from "../commands";
import type { CommandInfo, CommandResultData, SessionSummary } from "../api/types";

export function App() {
  const runtime = useXBot();
  const { state } = runtime;
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [newSessionOpen, setNewSessionOpen] = useState(false);
  const [commandOutput, setCommandOutput] = useState<CommandResultData | null>(null);
  const [helpQuery, setHelpQuery] = useState<string | null>(null);
  const [pendingCommand, setPendingCommand] = useState("");
  const [clearConfirmOpen, setClearConfirmOpen] = useState(false);
  const [deleteCandidate, setDeleteCandidate] = useState<SessionSummary | null>(null);
  const [composerDraft, setComposerDraft] = useState<{ id: number; value: string } | null>(null);
  const commands = useMemo(() => commandCatalog(runtime.commands), [runtime.commands]);

  useEffect(() => {
    setCommandOutput(null);
    setHelpQuery(null);
  }, [state.current?.session_id, state.current?.thread_id]);

  const sendComposerInput = async (
    content: string,
    attachments: PendingAttachment[],
  ): Promise<boolean> => {
    const parsed = parseCommand(content);
    if (!parsed) {
      setCommandOutput(null);
      return runtime.sendMessage(content, attachments);
    }
    const command = commands.find((item) => item.name === parsed.name);
    setCommandOutput(null);
    if (!command) {
      setCommandOutput(localCommandResult(parsed.name, `Unknown command: /${parsed.name}`));
      return true;
    }
    if (command.kind === "prompt") {
      return runtime.sendMessage(content, attachments);
    }
    if (attachments.length) {
      setCommandOutput(localCommandResult(parsed.name, "Client and server commands do not accept attachments."));
      return true;
    }
    if (command.kind === "server") {
      setPendingCommand(command.name);
      try {
        const result = await runtime.runServerCommand(command, content);
        if (result) setCommandOutput(result);
      } finally {
        setPendingCommand("");
      }
      return true;
    }
    await runClientCommand(parsed.name, parsed.args);
    return true;
  };

  const runClientCommand = async (name: string, args: string) => {
    if (name === "help") {
      setHelpQuery(args);
      return;
    }
    if (state.turnRunning && ["fork", "undo", "clear"].includes(name)) {
      setCommandOutput(localCommandResult(name, "Finish or interrupt the active turn before changing the session."));
      return;
    }
    if (name === "session") {
      const [action, remainder] = splitHead(args);
      if (!action || action === "list" || action === "ls") {
        await runtime.refreshSessions();
        setSidebarOpen(true);
      } else if (action === "new") {
        await runtime.createSession(unquote(remainder));
      } else {
        await runtime.resumeSession(action, unquote(remainder));
      }
      return;
    }
    if (name === "resume") {
      const [sessionId, workspace] = splitHead(args);
      await runtime.resumeSession(sessionId || undefined, unquote(workspace));
      return;
    }
    if (name === "new") {
      await runtime.createSession(unquote(args));
      return;
    }
    if (args && name !== "undo") {
      setCommandOutput(localCommandResult(name, `Usage: ${commands.find((item) => item.name === name)?.usage}`));
      return;
    }
    if (name === "fork") await runtime.fork();
    if (name === "clear") setClearConfirmOpen(true);
    if (name === "undo") {
      const count = args ? Number(args) : 1;
      if (!Number.isInteger(count) || count < 1) setCommandOutput(localCommandResult(name, "Undo count must be a positive integer."));
      else await runtime.undo(count);
    }
  };

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      if (deleteCandidate) {
        setDeleteCandidate(null);
        return;
      }
      if (clearConfirmOpen) {
        setClearConfirmOpen(false);
        return;
      }
      if (helpQuery === null && state.turnRunning && !state.interactions.length) {
        void runtime.interrupt();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [clearConfirmOpen, deleteCandidate, helpQuery, runtime, state.interactions.length, state.turnRunning]);

  return (
    <div className="app-shell">
      <SessionSidebar
        open={sidebarOpen}
        sessions={state.sessions}
        threads={state.threads}
        current={state.current}
        onClose={() => setSidebarOpen(false)}
        onNew={() => setNewSessionOpen(true)}
        onRefresh={runtime.refreshSessions}
        refreshing={state.loading}
        onSession={(id) => {
          setSidebarOpen(false);
          void runtime.resumeSession(id);
        }}
        onThread={(thread) => {
          setSidebarOpen(false);
          void runtime.selectThread(thread);
        }}
        onFork={(id) => {
          setSidebarOpen(false);
          void runtime.forkSession(id);
        }}
        onDelete={(session) => {
          setSidebarOpen(false);
          setDeleteCandidate(session);
        }}
      />
      {sidebarOpen && <button className="sidebar-scrim" aria-label="Close sidebar" onClick={() => setSidebarOpen(false)} />}

      <main className="workbench">
        <RuntimeHeader
          state={state}
          busy={runtime.commandRunning}
          onMenu={() => setSidebarOpen(true)}
          onAgent={runtime.selectAgent}
          onProvider={runtime.selectProvider}
          onEffort={runtime.selectEffort}
          onUndo={runtime.undo}
          onFork={runtime.fork}
          onClear={async () => setClearConfirmOpen(true)}
        />

        {state.error && (
          <div className="error-banner" role="alert">
            <AlertCircle size={16} />
            <span>{state.error}</span>
            {!state.eventStreamConnected && state.current && (
              <button className="text-button" onClick={() => void runtime.resumeSession()}>
                <RefreshCw size={14} /> Reconnect
              </button>
            )}
            <button className="icon-button small" title="Dismiss" aria-label="Dismiss error" onClick={runtime.clearError}>
              <X size={14} />
            </button>
          </div>
        )}

        {runtime.notification && (
          <div className="ui-notification" role="status">
            <AlertCircle size={15} />
            <span>{runtime.notification}</span>
            <button className="icon-button small" title="Dismiss" aria-label="Dismiss notification" onClick={runtime.clearNotification}>
              <X size={14} />
            </button>
          </div>
        )}

        {state.current ? (
          <>
            <Timeline
              key={`${state.current.session_id}/${state.current.thread_id}`}
              entries={state.entries}
              turnRunning={state.turnRunning}
              onRetry={runtime.retryLast}
              hasOlder={Boolean(state.historyCursor)}
              loadingOlder={state.historyLoading}
              onLoadOlder={runtime.loadEarlier}
            />
            <div className="runtime-controls">
              {pendingCommand && (
                <div className="command-progress" role="status">
                  <LoaderCircle size={13} className="spin" /> Running /{pendingCommand}
                </div>
              )}
              {commandOutput && (
                <CommandOutput
                  key={`${commandOutput.command}:${commandOutput.message.length}`}
                  result={commandOutput}
                  onClose={() => setCommandOutput(null)}
                />
              )}
              <TaskDock
                tasks={Object.values(state.tasks)}
                onStop={runtime.stopTask}
                onStopAll={runtime.stopAllTasks}
              />
              <TodoDock items={state.todos} />
              <Composer
                running={state.turnRunning}
                disabled={state.loading || runtime.commandRunning}
                queued={state.queuedMessages}
                commands={commands}
                draft={composerDraft}
                allowImages={Boolean(state.providers
                  .find((provider) => provider.name === state.current?.provider)
                  ?.models.find((model) => model.model === state.current?.model)
                  ?.input_modalities.includes("image"))}
                onSend={sendComposerInput}
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

      {helpQuery !== null && (
        <CommandHelpDialog
          commands={commands}
          initialQuery={helpQuery}
          onClose={() => setHelpQuery(null)}
          onSelect={(command) => {
            setHelpQuery(null);
            setComposerDraft((current) => ({
              id: (current?.id || 0) + 1,
              value: commandDraftValue(command),
            }));
          }}
        />
      )}

      {clearConfirmOpen && (
        <div className="dialog-backdrop" role="presentation" onMouseDown={(event) => {
          if (event.currentTarget === event.target) setClearConfirmOpen(false);
        }}>
          <section className="dialog clear-history-dialog" role="dialog" aria-modal="true" aria-labelledby="clear-history-title">
            <div className="dialog-heading">
              <div>
                <span className="eyebrow">Conversation history</span>
                <h2 id="clear-history-title">Clear this thread?</h2>
              </div>
              <button type="button" className="icon-button" title="Close" aria-label="Close clear confirmation" onClick={() => setClearConfirmOpen(false)}>
                <X size={17} />
              </button>
            </div>
            <p>This removes persisted messages from the current thread. Session settings, artifacts, and plugin state are preserved.</p>
            <div className="dialog-actions">
              <button type="button" className="secondary-button" autoFocus onClick={() => setClearConfirmOpen(false)}>Cancel</button>
              <button type="button" className="secondary-button danger" onClick={() => {
                setClearConfirmOpen(false);
                void runtime.clear();
              }}><Trash2 size={15} /> Clear history</button>
            </div>
          </section>
        </div>
      )}

      {deleteCandidate && (
        <div className="dialog-backdrop" role="presentation" onMouseDown={(event) => {
          if (event.currentTarget === event.target) setDeleteCandidate(null);
        }}>
          <section className="dialog delete-session-dialog" role="dialog" aria-modal="true" aria-labelledby="delete-session-title">
            <div className="dialog-heading">
              <div>
                <span className="eyebrow">Persisted session</span>
                <h2 id="delete-session-title">Delete this session?</h2>
              </div>
              <button type="button" className="icon-button" title="Close" aria-label="Close delete confirmation" onClick={() => setDeleteCandidate(null)}>
                <X size={17} />
              </button>
            </div>
            <p><strong>{deleteCandidate.title || deleteCandidate.session_id}</strong> and its persisted history, artifacts, and plugin state will be permanently deleted.</p>
            <code>{deleteCandidate.session_id}</code>
            <div className="dialog-actions">
              <button type="button" className="secondary-button" autoFocus onClick={() => setDeleteCandidate(null)}>Cancel</button>
              <button type="button" className="secondary-button danger" onClick={() => {
                const sessionId = deleteCandidate.session_id;
                setDeleteCandidate(null);
                void runtime.deleteSession(sessionId);
              }}><Trash2 size={15} /> Delete session</button>
            </div>
          </section>
        </div>
      )}

      {state.loading && <div className="loading-line" aria-label="Loading" />}
    </div>
  );
}

function localCommandResult(command: string, message: string): CommandResultData {
  return { command, status: "error", message, effects: [] };
}

function commandDraftValue(command: CommandInfo): string {
  return command.usage === command.slash ? command.slash : `${command.slash} `;
}

function splitHead(value: string): [string, string] {
  const input = value.trim();
  const separator = input.search(/\s/);
  if (separator < 0) return [input, ""];
  return [input.slice(0, separator), input.slice(separator).trim()];
}

function unquote(value: string): string {
  const input = value.trim();
  if (input.length > 1 && ((input.startsWith('"') && input.endsWith('"')) || (input.startsWith("'") && input.endsWith("'")))) {
    return input.slice(1, -1);
  }
  return input;
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
      <form
        className="dialog new-session-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="new-session-title"
        onKeyDown={(event) => {
          if (event.key === "Escape") onClose();
        }}
        onSubmit={(event) => {
          event.preventDefault();
          onCreate(workspace);
        }}
      >
        <div className="dialog-heading">
          <div>
            <span className="eyebrow">Session</span>
            <h2 id="new-session-title">New workspace</h2>
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
