import { useEffect, useMemo, useState } from "react";
import type { JsonObject, PluginConfigCatalog, PluginConfigScope } from "../api/types";
import { SchemaForm } from "./SchemaForm";
import { type JsonSchema, isRecord, schemaErrors } from "./schemaForm";

export interface PluginConfigPanelProps {
  sessionId?: string;
  threadId?: string;
  scope?: PluginConfigScope;
  load: (sessionId: string, threadId: string, scope: PluginConfigScope) => Promise<PluginConfigCatalog>;
  update: (sessionId: string, threadId: string, pluginId: string, scope: PluginConfigScope, revision: string, config: JsonObject) => Promise<PluginConfigCatalog>;
}

/** Recursive layer merge, matching how the server resolves a configuration. */
function mergeLayers(base: JsonObject, overlay: JsonObject): JsonObject {
  const merged: JsonObject = { ...base };
  for (const [key, value] of Object.entries(overlay)) {
    const current = merged[key];
    merged[key] = isRecord(current) && isRecord(value) ? mergeLayers(current, value) : value;
  }
  return merged;
}

/** In-progress edits for one plugin layer, valid only for its `key`. */
interface ConfigDraft {
  key: string;
  config: JsonObject;
  text: string;
  error: string;
}

export function PluginConfigPanel({ sessionId, threadId, scope: fixedScope, load, update }: PluginConfigPanelProps) {
  const [selectedScope, setSelectedScope] = useState<PluginConfigScope>(fixedScope || "workspace");
  const scope = fixedScope || selectedScope;
  const [catalog, setCatalog] = useState<PluginConfigCatalog | null>(null);
  const [selectedId, setSelectedId] = useState("");
  const [draft, setDraft] = useState<ConfigDraft | null>(null);
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

  // The draft is keyed by layer+plugin: a selection change simply falls back to
  // the stored layer instead of needing a synchronizing effect.
  const draftKey = `${scope}:${selected?.plugin_id ?? ""}`;
  const active = draft?.key === draftKey ? draft : null;
  const stored = selected?.scope_config ?? null;
  const config = active?.config ?? stored;
  const jsonDraft = active?.text ?? JSON.stringify(stored, null, 2);
  const jsonError = active?.error ?? "";

  const applyConfig = (next: JsonObject) => {
    setDraft({ key: draftKey, config: next, text: JSON.stringify(next, null, 2), error: "" });
  };

  const editJson = (text: string) => {
    try {
      const value: unknown = JSON.parse(text);
      if (!value || typeof value !== "object" || Array.isArray(value)) {
        throw new Error("Configuration must be a JSON object.");
      }
      setDraft({ key: draftKey, config: value as JsonObject, text, error: "" });
    } catch (error) {
      setDraft({
        key: draftKey,
        config: config ?? {},
        text,
        error: error instanceof Error ? error.message : String(error),
      });
    }
  };

  const save = async () => {
    if (!sessionId || !threadId || !catalog || !selected || !selected.editable || !config) return;
    setStatus("Saving…");
    try {
      const updated = await update(sessionId, threadId, selected.plugin_id, scope, catalog.revision, config);
      setCatalog(updated);
      setStatus("saved");
    } catch (error) {
      setStatus(error instanceof Error ? error.message : String(error));
    }
  };

  const schema = selected?.config_schema as JsonSchema | null | undefined;
  // The server validates the patch against the layers below it, so a value the
  // lower layers already supply is not a missing required field. The merge
  // mirrors the server's own layer merge.
  const gate = useMemo(
    () => (config ? mergeLayers(selected?.effective_config ?? {}, config) : null),
    [config, selected],
  );
  const errors = useMemo(
    () => (gate && schema?.properties ? schemaErrors(schema, gate) : {}),
    [gate, schema],
  );
  const errorCount = Object.keys(errors).length;
  const blocked = errorCount > 0 || Boolean(jsonError);

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
          {selected.editable && schema?.properties && config && (
            <SchemaForm
              schema={schema}
              value={config}
              onChange={applyConfig}
            />
          )}
          {selected.editable && errorCount > 0 && (
            <p className="schema-form-summary" role="alert">
              {errorCount === 1 ? "1 field needs attention before saving." : `${errorCount} fields need attention before saving.`}
            </p>
          )}
          {selected.editable && <details className="plugin-config-json-advanced">
            <summary>Advanced JSON</summary>
            <label className="plugin-config-json">
              <span>Layer configuration JSON</span>
              <textarea
                value={jsonDraft}
                spellCheck={false}
                onChange={(event) => editJson(event.target.value)}
                aria-label="Plugin configuration JSON"
              />
            </label>
            {jsonError && <small className="schema-field-error" role="alert">{jsonError}</small>}
          </details>}
          <details>
            <summary>Declared JSON Schema</summary>
            <pre>{JSON.stringify(selected.config_schema, null, 2)}</pre>
          </details>
          {selected.editable && <button type="button" className="primary-button" onClick={() => void save()} disabled={status === "Saving…" || blocked}>Save plugin configuration</button>}
          <small className="settings-save-status" aria-live="polite">{status === "saved" ? "Saved" : status !== "ready" && status !== "Saving…" ? status : ""}</small>
        </div>}
      </div>}
    </section>
  );
}
