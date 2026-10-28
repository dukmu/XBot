import { GitFork, Menu, RotateCcw, SlidersHorizontal, Trash2 } from "lucide-react";
import { type ReactNode, useState } from "react";
import type { RuntimeState } from "../state/runtime";

interface RuntimeHeaderProps {
  state: RuntimeState;
  busy: boolean;
  onMenu: () => void;
  onAgent: (name: string) => Promise<void>;
  onProvider: (name: string, model?: string) => Promise<void>;
  onEffort: (effort: string) => Promise<void>;
  onUndo: (count?: number) => Promise<void>;
  onFork: () => Promise<void>;
  onClear: () => Promise<void>;
  utilities?: ReactNode;
  /** Which conversation surface is on screen, as the ported banner's tabs. */
  surface?: "chat" | "trajectory";
  onSurface?: (surface: "chat" | "trajectory") => void;
}

export function RuntimeHeader({ state, busy, onMenu, onAgent, onProvider, onEffort, onUndo, onFork, onClear, utilities, surface = "chat", onSurface }: RuntimeHeaderProps) {
  const current = state.current;
  const [mobileSettings, setMobileSettings] = useState(false);
  // Agent/model/effort live in the composer now (ported placement); the
  // mobile menu below keeps its own compact copies for narrow screens.
  const providerValue = current
    ? JSON.stringify([current.provider, current.model])
    : "";
  return (
    <header className="runtime-header">
      <button className="icon-button menu-button" title="Sessions" aria-label="Open sessions" onClick={onMenu}>
        <Menu size={18} />
      </button>
      <div className="runtime-title">
        <strong>{current ? current.title : "XBot"}</strong>
        {current && <span title={current.workspace_root}>{current.workspace_root}</span>}
      </div>
      {current && onSurface && (
        <div className="runtime-tabs" role="tablist" aria-label="Conversation surface">
          <button
            type="button"
            role="tab"
            aria-selected={surface === "chat"}
            className={surface === "chat" ? "active" : ""}
            onClick={() => onSurface("chat")}
          >
            Chat
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={surface === "trajectory"}
            className={surface === "trajectory" ? "active" : ""}
            onClick={() => onSurface("trajectory")}
          >
            Trajectory
          </button>
        </div>
      )}
      {current && utilities && <div className="header-utilities">{utilities}</div>}
      {current && (
        <button
          className="icon-button mobile-runtime-button"
          title="Runtime settings"
          aria-label="Runtime settings"
          onClick={() => setMobileSettings((open) => !open)}
        >
          <SlidersHorizontal size={16} />
        </button>
      )}
      {current && mobileSettings && (
        <div className="mobile-runtime-menu">
          <div className="mobile-runtime-workspace">
            <span>Workspace</span>
            <code title={current.workspace_root}>{current.workspace_root}</code>
          </div>
          <label>
            <span>Agent</span>
            <select
              value={current.agent_name}
              disabled={state.turnRunning || state.loading || busy}
              onChange={(event) => {
                setMobileSettings(false);
                void onAgent(event.target.value);
              }}
            >
              {state.agents.filter((agent) => agent.mode !== "subagent").map((agent) => (
                <option key={agent.name} value={agent.name}>{agent.name}</option>
              ))}
            </select>
          </label>
          <label>
            <span>Provider</span>
            <select
              value={providerValue}
              disabled={state.turnRunning || state.loading || busy}
              onChange={(event) => {
                setMobileSettings(false);
                const [provider, model] = JSON.parse(event.target.value) as [string, string];
                void onProvider(provider, model);
              }}
            >
              {state.providers.flatMap((provider) => provider.models.map((model) => (
                <option key={`${provider.name}/${model.model}`} value={JSON.stringify([provider.name, model.model])}>
                  {provider.name} / {model.model}
                </option>
              )))}
            </select>
          </label>
        </div>
      )}
      {current && (
        <div className="header-actions">
          <button className="icon-button" title="Undo last turn" aria-label="Undo last turn" disabled={state.turnRunning || state.loading || busy} onClick={() => void onUndo(1)}>
            <RotateCcw size={16} />
          </button>
          <button className="icon-button" title="Fork session" aria-label="Fork session" disabled={state.turnRunning || state.loading || busy} onClick={() => void onFork()}>
            <GitFork size={16} />
          </button>
          <button className="icon-button danger-hover" title="Clear history" aria-label="Clear history" disabled={state.turnRunning || state.loading || busy} onClick={() => {
            void onClear();
          }}>
            <Trash2 size={16} />
          </button>
        </div>
      )}
    </header>
  );
}
