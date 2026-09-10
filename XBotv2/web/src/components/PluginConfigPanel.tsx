import { useEffect, useMemo, useState } from "react";
import type { JsonObject, PluginConfigCatalog, PluginConfigScope } from "../api/types";

type JsonSchema = {
  type?: string;
  title?: string;
  description?: string;
  enum?: unknown[];
  properties?: Record<string, JsonSchema>;
};

export interface PluginConfigPanelProps {
  sessionId?: string;
  threadId?: string;
  scope?: PluginConfigScope;
  load: (sessionId: string, threadId: string, scope: PluginConfigScope) => Promise<PluginConfigCatalog>;
  update: (sessionId: string, threadId: string, pluginId: string, scope: PluginConfigScope, revision: string, config: JsonObject) => Promise<PluginConfigCatalog>;
}

export function PluginConfigPanel({ sessionId, threadId, scope: fixedScope, load, update }: PluginConfigPanelProps) {
  const [selectedScope, setSelectedScope] = useState<PluginConfigScope>(fixedScope || "workspace");
  const scope = fixedScope || selectedScope;
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

  const parsedDraft = useMemo(() => {
    try {
      const value: unknown = JSON.parse(draft);
      return value && typeof value === "object" && !Array.isArray(value)
        ? value as Record<string, unknown>
        : null;
    } catch {
      return null;
    }
  }, [draft]);

  const schema = selected?.config_schema as JsonSchema | null | undefined;
  const setField = (name: string, value: unknown) => {
    if (!parsedDraft) return;
    setDraft(JSON.stringify({ ...parsedDraft, [name]: value }, null, 2));
  };

  return (
    <section className="settings-section plugin-config-panel" aria-labelledby="plugin-config-title">
      <div className="settings-section-heading">
        <div>
          <h3 id="plugin-config-title">Plugin configuration</h3>
          <p>Declarations and schemas come from the loaded plugins. Global/workspace changes apply on later starts; session changes apply to this session's next runtime.</p>
        </div>
      </div>
      <div className="plugin-config-toolbar">
        {!fixedScope ? <label><span>Scope</span><select value={scope} onChange={(event) => setSelectedScope(event.target.value as PluginConfigScope)}>
          <option value="workspace">Workspace</option>
          <option value="global">Global</option>
          <option value="session">Session</option>
        </select></label> : <span className="plugin-config-scope">{scope === "global" ? "Global" : scope === "session" ? "Session" : "Workspace"}</span>}
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
          {selected.editable && schema?.properties && parsedDraft && (
            <div className="plugin-config-fields" aria-label="Schema fields">
              {Object.entries(schema.properties).map(([name, field]) => {
                const value = parsedDraft[name];
                const label = field.title || name;
                if (field.enum?.length) {
                  return <label key={name}><span>{label}</span><select
                    aria-label={label}
                    value={String(value ?? "")}
                    onChange={(event) => setField(name, event.target.value)}
                  >
                    {field.enum.map((option) => <option key={String(option)} value={String(option)}>{String(option)}</option>)}
                  </select>{field.description && <small>{field.description}</small>}</label>;
                }
                if (field.type === "boolean") {
                  return <label key={name} className="plugin-config-checkbox"><span>{label}</span><input
                    type="checkbox"
                    aria-label={label}
                    checked={value === true}
                    onChange={(event) => setField(name, event.target.checked)}
                  />{field.description && <small>{field.description}</small>}</label>;
                }
                if (field.type === "number" || field.type === "integer") {
                  return <label key={name}><span>{label}</span><input
                    type="number"
                    aria-label={label}
                    value={typeof value === "number" ? value : ""}
                    onChange={(event) => setField(name, event.target.value === "" ? null : Number(event.target.value))}
                  />{field.description && <small>{field.description}</small>}</label>;
                }
                if (field.type === "string") {
                  return <label key={name}><span>{label}</span><input
                    type="text"
                    aria-label={label}
                    value={typeof value === "string" ? value : ""}
                    onChange={(event) => setField(name, event.target.value)}
                  />{field.description && <small>{field.description}</small>}</label>;
                }
                return <label key={name} className="plugin-config-json"><span>{label} (JSON)</span><textarea
                  aria-label={`${label} JSON`}
                  value={JSON.stringify(value ?? null, null, 2)}
                  spellCheck={false}
                  onChange={(event) => {
                    try { setField(name, JSON.parse(event.target.value)); } catch { /* save reports malformed JSON */ }
                  }}
                />{field.description && <small>{field.description}</small>}</label>;
              })}
            </div>
          )}
          {selected.editable && <details className="plugin-config-json-advanced">
            <summary>Advanced JSON</summary>
            <label className="plugin-config-json">
              <span>Layer configuration JSON</span>
              <textarea
                value={draft}
                spellCheck={false}
                onChange={(event) => setDraft(event.target.value)}
                aria-label="Plugin configuration JSON"
              />
            </label>
          </details>}
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
