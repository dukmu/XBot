import { GitFork, Menu, RotateCcw, SlidersHorizontal, Trash2 } from "lucide-react";
import { useState } from "react";
import type { RuntimeState } from "../state/runtime";

interface RuntimeHeaderProps {
  state: RuntimeState;
  onMenu: () => void;
  onAgent: (name: string) => Promise<void>;
  onProvider: (name: string) => Promise<void>;
  onUndo: (count?: number) => Promise<void>;
  onFork: () => Promise<void>;
  onClear: () => Promise<void>;
}

export function RuntimeHeader({ state, onMenu, onAgent, onProvider, onUndo, onFork, onClear }: RuntimeHeaderProps) {
  const current = state.current;
  const [mobileSettings, setMobileSettings] = useState(false);
  return (
    <header className="runtime-header">
      <button className="icon-button menu-button" title="Sessions" aria-label="Open sessions" onClick={onMenu}>
        <Menu size={18} />
      </button>
      <div className="runtime-title">
        <strong>{current ? threadTitle(current.thread_id, current.agent_name) : "XBot"}</strong>
        {current && <span title={current.workspace_root}>{current.workspace_root}</span>}
      </div>
      {current && (
        <div className="runtime-selectors">
          <select
            aria-label="Agent"
            title="Agent"
            value={current.agent_name}
            disabled={state.turnRunning}
            onChange={(event) => void onAgent(event.target.value)}
          >
            {state.agents.filter((agent) => agent.mode !== "subagent").map((agent) => (
              <option key={agent.name} value={agent.name}>{agent.name}</option>
            ))}
          </select>
          <select
            aria-label="Provider"
            title="Provider"
            value={current.provider}
            disabled={state.turnRunning}
            onChange={(event) => void onProvider(event.target.value)}
          >
            {state.providers.map((provider) => (
              <option key={provider.name} value={provider.name}>{provider.name}</option>
            ))}
          </select>
        </div>
      )}
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
          <label>
            <span>Agent</span>
            <select
              value={current.agent_name}
              disabled={state.turnRunning}
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
              value={current.provider}
              disabled={state.turnRunning}
              onChange={(event) => {
                setMobileSettings(false);
                void onProvider(event.target.value);
              }}
            >
              {state.providers.map((provider) => (
                <option key={provider.name} value={provider.name}>{provider.name}</option>
              ))}
            </select>
          </label>
        </div>
      )}
      {current && (
        <div className="header-actions">
          <button className="icon-button" title="Undo last turn" aria-label="Undo last turn" disabled={state.turnRunning} onClick={() => void onUndo(1)}>
            <RotateCcw size={16} />
          </button>
          <button className="icon-button" title="Fork session" aria-label="Fork session" disabled={state.turnRunning} onClick={() => void onFork()}>
            <GitFork size={16} />
          </button>
          <button className="icon-button danger-hover" title="Clear history" aria-label="Clear history" disabled={state.turnRunning} onClick={() => {
            if (window.confirm("Clear this thread's conversation history?")) void onClear();
          }}>
            <Trash2 size={16} />
          </button>
        </div>
      )}
    </header>
  );
}

function threadTitle(threadId: string, agent: string): string {
  return threadId === "agent" ? agent || "agent" : `${agent || "agent"} / ${threadId}`;
}
