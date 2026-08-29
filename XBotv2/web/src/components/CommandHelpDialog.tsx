import { Search, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import type { CommandInfo } from "../api/types";

export function CommandHelpDialog({
  commands,
  initialQuery,
  onClose,
  onSelect,
}: {
  commands: CommandInfo[];
  initialQuery: string;
  onClose: () => void;
  onSelect: (command: CommandInfo) => void;
}) {
  const [query, setQuery] = useState(initialQuery.replace(/^\//, ""));
  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return commands;
    return commands.filter((command) => (
      command.name.includes(needle)
      || command.description.toLowerCase().includes(needle)
      || command.usage.toLowerCase().includes(needle)
    ));
  }, [commands, query]);

  useEffect(() => {
    const close = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, [onClose]);

  return (
    <div className="dialog-backdrop" role="presentation" onMouseDown={(event) => {
      if (event.currentTarget === event.target) onClose();
    }}>
      <section className="dialog command-help-dialog" role="dialog" aria-modal="true" aria-labelledby="command-help-title">
        <div className="dialog-heading">
          <div>
            <span className="eyebrow">Command directory</span>
            <h2 id="command-help-title">Commands</h2>
          </div>
          <button type="button" className="icon-button" title="Close" aria-label="Close command help" onClick={onClose}>
            <X size={17} />
          </button>
        </div>
        <label className="command-help-search">
          <Search size={14} />
          <input autoFocus value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search name, description, or usage" />
        </label>
        <div className="command-help-list">
          {(["client", "server", "prompt"] as const).map((kind) => {
            const items = visible.filter((command) => command.kind === kind);
            if (items.length === 0) return null;
            return (
              <section className="command-help-group" key={kind} aria-label={`${kind} commands`}>
                <h3>{kind}</h3>
                {items.map((command) => (
                  <article key={`${command.kind}:${command.name}`}>
                    <button type="button" onClick={() => onSelect(command)}>
                      <div><b>{command.slash}</b><span className="command-help-use">Use</span></div>
                      <p>{command.description}</p>
                      <code>{command.usage}</code>
                      {command.examples.length > 0 && <small>{command.examples.join(" · ")}</small>}
                    </button>
                  </article>
                ))}
              </section>
            );
          })}
          {visible.length === 0 && <div className="command-help-empty">No matching commands</div>}
        </div>
      </section>
    </div>
  );
}
