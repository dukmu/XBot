import { useEffect, useMemo, useState } from "react";
import type { JsonObject, PluginConfigCatalog, PluginConfigScope } from "../api/types";

export interface PluginConfigPanelProps {
  sessionId?: string;
  threadId?: string;
  load: (sessionId: string, threadId: string, scope: PluginConfigScope) => Promise<PluginConfigCatalog>;
  update: (sessionId: string, threadId: string, pluginId: string, scope: PluginConfigScope, revision: string, config: JsonObject) => Promise<PluginConfigCatalog>;
}

export function PluginConfigPanel({ sessionId, threadId, load, update }: PluginConfigPanelProps) {
  const [scope, setScope] = useState<PluginConfigScope>("workspace");
  const [catalog, setCatalog] = useState<PluginConfigCatalog | null>(null);
  const [selectedId, setSelectedId] = useState("");
  const [draft, setDraft] = useState("{}");
  const [status, setStatus] = useState("idle");

  useEffect(() => {
    if (!sessionId || !threadId) {
      setCatalog(null);
      setStatus("Open a session to edit plugin configuration.");
      return;
    }
    let alive = true;
    setStatus("Loading plugin declarations…");
    void load(sessionId, threadId, scope).then((value) => {
      if (!alive) return;
      setCatalog(value);
      setSelectedId((current) => current && value.plugins.some((item) => item.plugin_id === current)
        ? current
        : value.plugins[0]?.plugin_id || "");
      setStatus("ready");
    }).catch((error) => {
      if (alive) setStatus(error instanceof Error ? error.message : String(error));
    });
    return () => { alive = false; };
  }, [load, scope, sessionId, threadId]);

  const selected = useMemo(
    () => catalog?.plugins.find((item) => item.plugin_id === selectedId) || null,
    [catalog, selectedId],
  );

  useEffect(() => {
    setDraft(selected ? JSON.stringify(selected.scope_config, null, 2) : "{}");
  }, [selected]);

  const save = async () => {
    if (!sessionId || !threadId || !catalog || !selected || !selected.editable) return;
    let config: JsonObject;
    try {
      const value: unknown = JSON.parse(draft);
      if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Configuration must be a JSON object.");
      config = value as JsonObject;
    } catch (error) {
      setStatus(error instanceof Error ? error.message : String(error));
      return;
    }
    setStatus("Saving…");
    try {
      const updated = await update(sessionId, threadId, selected.plugin_id, scope, catalog.revision, config);
      setCatalog(updated);
      setStatus("saved");
    } catch (error) {
      setStatus(error instanceof Error ? error.message : String(error));
    }
  };

  return (
    <section className="settings-section plugin-config-panel" aria-labelledby="plugin-config-title">
      <div className="settings-section-heading">
        <div>
          <h3 id="plugin-config-title">Plugin configuration</h3>
          <p>Declarations and schemas come from the loaded plugins. Changes apply to new sessions.</p>
        </div>
      </div>
      <div className="plugin-config-toolbar">
        <label><span>Scope</span><select value={scope} onChange={(event) => setScope(event.target.value as PluginConfigScope)}>
          <option value="workspace">Workspace</option>
          <option value="global">Global</option>
        </select></label>
        {catalog && <small>Revision {catalog.revision.slice(0, 12)}</small>}
      </div>
      {!catalog && <p className="settings-save-status" aria-live="polite">{status}</p>}
      {catalog && <div className="plugin-config-layout">
        <nav className="plugin-config-list" aria-label="Plugin configurations">
          {catalog.plugins.map((plugin) => <button
            type="button"
            key={plugin.plugin_id}
            className={plugin.plugin_id === selectedId ? "selected" : ""}
            onClick={() => setSelectedId(plugin.plugin_id)}
          >
            <strong>{plugin.plugin_id}</strong>
            <small>{plugin.editable ? "Schema available" : "Not configurable"}</small>
          </button>)}
        </nav>
        {selected && <div className="plugin-config-editor">
          <div className="plugin-config-editor-heading">
            <strong>{selected.name}</strong>
            {!selected.editable && <small>{selected.unavailable_reason}</small>}
          </div>
          <label className="plugin-config-json">
            <span>Layer configuration JSON</span>
            <textarea
              value={draft}
              disabled={!selected.editable}
              spellCheck={false}
              onChange={(event) => setDraft(event.target.value)}
              aria-label="Plugin configuration JSON"
            />
          </label>
          <details>
            <summary>Declared JSON Schema</summary>
            <pre>{JSON.stringify(selected.config_schema, null, 2)}</pre>
          </details>
          {selected.editable && <button type="button" className="primary-button" onClick={() => void save()} disabled={status === "Saving…"}>Save plugin configuration</button>}
          <small className="settings-save-status" aria-live="polite">{status === "saved" ? "Saved" : status !== "ready" && status !== "Saving…" ? status : ""}</small>
        </div>}
      </div>}
    </section>
  );
}
