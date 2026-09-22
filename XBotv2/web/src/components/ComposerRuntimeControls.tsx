/* Model / agent / reasoning selectors, moved out of the header and into the
 * composer, which is where the WebUI these are ported from keeps them. */
import { useCallback, useState } from "react";
import { Check, ChevronDown } from "lucide-react";
import type { AgentInfo, ProviderInfo } from "../api/types";
import type { RuntimeSession } from "../state/runtime";

export function ComposerRuntimeControls({
  current,
  agents,
  providers,
  disabled,
  onAgent,
  onProvider,
  onEffort,
}: {
  /** Null before a session exists: the draft session shows them locked. */
  current: RuntimeSession | null;
  agents: AgentInfo[];
  providers: ProviderInfo[];
  disabled: boolean;
  onAgent: (name: string) => Promise<void>;
  onProvider: (name: string, model?: string) => Promise<void>;
  onEffort: (effort: string) => Promise<void>;
}) {
  const selectedModel = current
    ? providers
      .find((provider) => provider.name === current.provider)
      ?.models.find((model) => model.model === current.model)
    : providers[0]?.models.find((model) => model.model === providers[0]?.default_model);
  const providerValue = current
    ? JSON.stringify([current.provider, current.model])
    : (selectedModel && providers[0]
      ? JSON.stringify([providers[0].name, selectedModel.model])
      : "");
  const modelLabel = current
    ? `${current.provider} / ${current.model}`
    : "Select model";

  return (
    <div className="composer-runtime-controls">
      <AgentPresetMenu
        current={current?.agent_name ?? ""}
        agents={agents}
        disabled={disabled}
        onAgent={onAgent}
      />
      <select
        className="composer-select"
        aria-label={`Select model, current ${modelLabel}`}
        title={`Select model, current ${modelLabel}`}
        value={providerValue}
        disabled={disabled}
        onChange={(event) => {
          const [provider, model] = JSON.parse(event.target.value) as [string, string];
          void onProvider(provider, model);
        }}
      >
        {providers.flatMap((provider) => provider.models.map((model) => (
          <option key={`${provider.name}/${model.model}`} value={JSON.stringify([provider.name, model.model])}>
            {modelLabel === `${provider.name} / ${model.model}` ? model.model : `${provider.name} / ${model.model}`}
          </option>
        )))}
      </select>
      {selectedModel && selectedModel.effort.length > 0 && (
        <EffortMenu
          effort={current?.model_mode ?? ""}
          options={selectedModel.effort}
          disabled={disabled}
          onEffort={onEffort}
        />
      )}
    </div>
  );
}

/**
 * Agent-preset selection, ported: the trigger names the running mode, and the
 * menu lists every primary definition with the description the catalog gives it
 * (subagent definitions are not selectable as the running mode).
 */
export function AgentPresetMenu({
  current,
  agents,
  disabled,
  onAgent,
}: {
  current: string;
  agents: AgentInfo[];
  disabled: boolean;
  onAgent: (name: string) => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const selectable = agents.filter((agent) => agent.mode !== "subagent");
  const label = current ? `Standard mode, current: ${current}` : "Standard mode";
  const close = useCallback(() => setOpen(false), []);

  if (selectable.length === 0) {
    // Nothing to choose from: the draft session still shows the ported trigger.
    return (
      <button type="button" className="composer-select composer-mode-select" disabled aria-label={label} title="Standard mode">
        {current || "Standard mode"} <ChevronDown size={13} />
      </button>
    );
  }

  return (
    <div className="composer-preset" onBlur={(event) => {
      if (!event.currentTarget.contains(event.relatedTarget)) close();
    }}>
      <button
        type="button"
        className="composer-select composer-mode-select"
        aria-label={label}
        aria-haspopup="menu"
        aria-expanded={open}
        title="Standard mode"
        disabled={disabled}
        onClick={() => setOpen((value) => !value)}
        onKeyDown={(event) => {
          if (event.key === "Escape") close();
        }}
      >
        {current || "Standard mode"} <ChevronDown size={13} />
      </button>
      {open && (
        <div className="composer-preset-menu" role="menu" aria-label="Standard mode">
          {selectable.map((agent) => (
            <button
              type="button"
              role="menuitem"
              key={agent.name}
              className={agent.name === current ? "active" : ""}
              aria-current={agent.name === current ? "true" : undefined}
              onClick={() => {
                close();
                if (agent.name !== current) void onAgent(agent.name);
              }}
            >
              <span className="composer-preset-name">
                {agent.name}
                {agent.name === current && <Check size={12} aria-hidden />}
              </span>
              {agent.description && <small>{agent.description}</small>}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/** Ported effort menu copy: the explicit default sits beside the model's own levels. */
export const EFFORT_LABEL = "Reasoning effort";
export const EFFORT_DEFAULT = "Default";

/**
 * Reasoning effort, ported as a radio menu: an explicit `Default` (the model's
 * own level) beside the levels the catalog declares for the running model.
 */
export function EffortMenu({
  effort,
  options,
  disabled,
  onEffort,
}: {
  effort: string;
  options: string[];
  disabled: boolean;
  onEffort: (effort: string) => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const levels = [EFFORT_DEFAULT, ...options.filter((option) => option !== EFFORT_DEFAULT)];
  const current = effort || EFFORT_DEFAULT;
  return (
    <div className="composer-preset" onBlur={(event) => {
      if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false);
    }}>
      <button
        type="button"
        className="composer-select composer-effort-select"
        aria-label={`${EFFORT_LABEL}, current: ${current}`}
        aria-haspopup="menu"
        aria-expanded={open}
        title={EFFORT_LABEL}
        disabled={disabled}
        onClick={() => setOpen((value) => !value)}
        onKeyDown={(event) => {
          if (event.key === "Escape") setOpen(false);
        }}
      >
        {current} <ChevronDown size={13} />
      </button>
      {open && (
        <div className="composer-preset-menu" role="menu" aria-label={EFFORT_LABEL}>
          {levels.map((level) => (
            <button
              type="button"
              role="menuitemradio"
              key={level}
              aria-checked={level === current}
              className={level === current ? "active" : ""}
              onClick={() => {
                setOpen(false);
                if (level !== current) void onEffort(level === EFFORT_DEFAULT ? "" : level);
              }}
            >
              <span className="composer-preset-name">
                {level}
                {level === current && <Check size={12} aria-hidden />}
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
