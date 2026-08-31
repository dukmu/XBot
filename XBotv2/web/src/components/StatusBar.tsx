import type { RuntimeState } from "../state/runtime";

export function StatusBar({ state }: { state: RuntimeState }) {
  const current = state.current;
  if (!current) {
    const reachable = state.serverReachable;
    const ready = reachable && state.catalogEventStreamConnected;
    return (
      <footer className="status-bar">
        <span className={`connection-state ${ready ? "online" : "offline"}`} />
        <span>{
          !reachable
            ? "Server unavailable"
            : ready ? "Server ready" : "Server ready · reconnecting session updates"
        }</span>
      </footer>
    );
  }
  const model = [current.provider, current.model].filter(Boolean).join("/") + (current.model_mode ? `:${current.model_mode}` : "");
  const contextFree = current.context_window > 0
    ? Math.max(0, Math.round((1 - state.usage.context_tokens / current.context_window) * 100))
    : null;
  const fullInput = state.usage.input_tokens
    + state.usage.cache_read_input_tokens
    + state.usage.cache_creation_input_tokens
    + state.usage.prompt_cache_write_tokens;
  const sessionLive = state.serverReachable && state.sessionAttached && state.eventStreamConnected;
  const live = sessionLive && state.catalogEventStreamConnected;
  const connectionTitle = !state.sessionAttached
    ? "Session detached"
    : `${sessionLive ? "Session events connected" : "Session events disconnected"} · ${
      state.catalogEventStreamConnected ? "session catalog connected" : "session catalog reconnecting"
    }`;
  return (
    <footer className="status-bar">
      <span className={`connection-state ${live ? "online" : "offline"}`} title={connectionTitle} />
      <span className="status-agent">agent:{current.agent_name}</span>
      <span className="status-model" title={model}>{model}</span>
      {Object.entries(current.status_slots).map(([name, value]) => (
        <span className="status-slot" key={name}>{name}:{value}</span>
      ))}
      <span className="status-spacer" />
      {contextFree !== null && <span className="status-context">ctx-free:{contextFree}%</span>}
      <span className="status-tokens" title={`${fullInput} in / ${state.usage.output_tokens} out · ${state.usage.cache_read_input_tokens} cache read`}>
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
