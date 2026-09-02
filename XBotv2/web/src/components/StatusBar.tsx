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
    </footer>
  );
}
