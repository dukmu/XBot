import type { RuntimeState } from "../state/runtime";

export function StatusBar({ state }: { state: RuntimeState }) {
  const current = state.current;
  if (!current) {
    return (
      <footer className="status-bar">
        <span className={`connection-state ${state.connected ? "online" : "offline"}`} />
        <span>{state.connected ? "Connected" : "Disconnected"}</span>
      </footer>
    );
  }
  const model = [current.provider, current.model].filter(Boolean).join("/") + (current.model_mode ? `:${current.model_mode}` : "");
  const contextFree = current.context_window > 0
    ? Math.max(0, Math.round((1 - state.usage.context_tokens / current.context_window) * 100))
    : null;
  return (
    <footer className="status-bar">
      <span className={`connection-state ${state.connected ? "online" : "offline"}`} title={state.connected ? "Connected" : "Disconnected"} />
      <span className="status-agent">agent:{current.agent_name}</span>
      <span className="status-model" title={model}>{model}</span>
      {Object.entries(current.status_slots).map(([name, value]) => (
        <span className="status-slot" key={name}>{name}:{value}</span>
      ))}
      <span className="status-spacer" />
      {contextFree !== null && <span className="status-context">ctx-free:{contextFree}%</span>}
      <span className="status-tokens" title={`${state.usage.input_tokens} in / ${state.usage.output_tokens} out`}>
        tokens:{compact(state.usage.total_tokens)}
      </span>
    </footer>
  );
}

export function compact(value: number): string {
  if (value < 1_000) return String(value);
  if (value < 1_000_000) return `${trim(value / 1_000)}k`;
  return `${trim(value / 1_000_000)}M`;
}

function trim(value: number): string {
  return value.toFixed(1).replace(/\.0$/, "");
}
