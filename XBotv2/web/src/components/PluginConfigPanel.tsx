import { useEffect, useMemo, useState } from "react";
import { Search } from "lucide-react";
import type { JsonObject, PluginConfigCatalog, PluginConfigDescriptor, PluginConfigScope } from "../api/types";
import { SchemaForm } from "./SchemaForm";
import { type JsonSchema, isRecord, schemaErrors } from "./schemaForm";

export interface PluginConfigPanelProps {
  sessionId?: string;
  threadId?: string;
  scope?: PluginConfigScope;
  load: (sessionId: string, threadId: string, scope: PluginConfigScope) => Promise<PluginConfigCatalog>;
  update: (sessionId: string, threadId: string, pluginId: string, scope: PluginConfigScope, revision: string, config: JsonObject) => Promise<PluginConfigCatalog>;
}

/** In-progress edits for one plugin layer, valid only for its `key`. */
interface ConfigDraft {
  key: string;
  config: JsonObject;
  text: string;
  error: string;
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

function scopeTitle(scope: PluginConfigScope): string {
  return scope === "global" ? "Global" : scope === "session" ? "Session" : "Workspace";
}

export function PluginConfigPanel({ sessionId, threadId, scope: fixedScope, load, update }: PluginConfigPanelProps) {
  const [selectedScope, setSelectedScope] = useState<PluginConfigScope>(fixedScope || "workspace");
  const scope = fixedScope || selectedScope;
  const [catalog, setCatalog] = useState<PluginConfigCatalog | null>(null);
  const [selectedId, setSelectedId] = useState("");
  const [draft, setDraft] = useState<ConfigDraft | null>(null);
  const [query, setQuery] = useState("");
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

  const plugins = useMemo(() => catalog?.plugins ?? [], [catalog]);
  const configurable = useMemo(() => plugins.filter((plugin) => plugin.editable), [plugins]);
  const silent = useMemo(() => plugins.filter((plugin) => !plugin.editable), [plugins]);
  const term = query.trim().toLowerCase();
  const listed = term
    ? plugins.filter((plugin) => `${plugin.plugin_id} ${plugin.name}`.toLowerCase().includes(term))
    : configurable;

  const selected = useMemo(
    () => plugins.find((plugin) => plugin.plugin_id === selectedId) ?? null,
    [plugins, selectedId],
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
      setStatus("Saved");
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
  const dirty = JSON.stringify(config) !== JSON.stringify(stored) || Boolean(jsonError);
  const saving = status === "Saving…";
  const canSave = Boolean(selected?.editable) && dirty && errorCount === 0 && !jsonError && !saving;
  const statusLine = dirty
    ? errorCount === 1
      ? "1 field needs attention"
      : errorCount > 1
        ? `${errorCount} fields need attention`
        : "Unsaved changes"
    : status === "ready" || status === "idle" ? "" : status;

  const renderPlugin = (plugin: PluginConfigDescriptor) => (
    <button
      type="button"
      key={plugin.plugin_id}
      className={plugin.plugin_id === selectedId ? "selected" : ""}
      aria-current={plugin.plugin_id === selectedId ? "true" : undefined}
      onClick={() => setSelectedId(plugin.plugin_id)}
    >
      <strong>{plugin.plugin_id}{draft?.key === `${scope}:${plugin.plugin_id}` && <span className="plugin-config-dot" aria-hidden="true" />}</strong>
      <small>{plugin.editable ? "Schema available" : "Not configurable"}</small>
    </button>
  );

  return (
    <section className="settings-section plugin-config-panel" aria-labelledby="plugin-config-title">
      <div className="settings-section-heading">
        <div>
          <h3 id="plugin-config-title">Plugin configuration</h3>
          <p>Each plugin declares the configuration it accepts. A layer stores only what that scope overrides.</p>
        </div>
        <div className="plugin-config-heading-meta">
          {!fixedScope && (
            <label>
              <span>Scope</span>
              <select value={scope} onChange={(event) => setSelectedScope(event.target.value as PluginConfigScope)}>
                <option value="workspace">Workspace</option>
                <option value="global">Global</option>
                <option value="session">Session</option>
              </select>
            </label>
          )}
          {catalog && <small className="plugin-config-revision" title={catalog.revision}>revision {catalog.revision.slice(0, 12)}</small>}
        </div>
      </div>

      {!catalog && <p className="settings-save-status" aria-live="polite">{status}</p>}

      {catalog && (
        <div className="plugin-config-layout">
          <aside className="plugin-config-browser">
            <label className="plugin-config-filter">
              <Search size={13} aria-hidden="true" />
              <input
                type="search"
                value={query}
                aria-label="Filter plugins"
                placeholder="Filter plugins"
                onChange={(event) => setQuery(event.target.value)}
              />
            </label>
            <nav className="plugin-config-list" aria-label="Plugin configurations">
              {listed.map(renderPlugin)}
            </nav>
            {term && !listed.length && <p className="plugin-config-hint">No plugin matches “{query.trim()}”.</p>}
            {!term && !configurable.length && <p className="plugin-config-hint">No plugin declares a configuration schema.</p>}
            {!term && silent.length > 0 && (
              <details className="plugin-config-silent">
                <summary>
                  {silent.length === 1
                    ? "1 plugin declares no configuration"
                    : `${silent.length} plugins declare no configuration`}
                </summary>
                <nav className="plugin-config-list" aria-label="Plugins without configuration">
                  {silent.map(renderPlugin)}
                </nav>
              </details>
            )}
          </aside>

          {selected && (
            <div className="plugin-config-editor">
              <header className="plugin-config-editor-bar">
                <div className="plugin-config-editor-title">
                  <strong>{selected.name}</strong>
                  <small>{selected.editable ? `${scopeTitle(scope)} layer` : "Read only"}</small>
                </div>
                <div className="plugin-config-editor-actions">
                  <small className={`plugin-config-status${dirty ? " dirty" : ""}`} aria-live="polite">{statusLine}</small>
                  {selected.editable && dirty && (
                    <button type="button" className="secondary-button" onClick={() => setDraft(null)}>Discard</button>
                  )}
                  {selected.editable && (
                    <button type="button" className="primary-button" disabled={!canSave} onClick={() => void save()}>
                      Save plugin configuration
                    </button>
                  )}
                </div>
              </header>

              {!selected.editable && (
                <p className="plugin-config-unavailable">
                  {selected.unavailable_reason || "This plugin declares no configuration schema."}
                </p>
              )}
              {selected.editable && schema?.properties && config && (
                <SchemaForm schema={schema} value={config} onChange={applyConfig} />
              )}
              {selected.editable && (
                <details className="plugin-config-advanced">
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
                </details>
              )}
              <details className="plugin-config-advanced">
                <summary>Declared JSON Schema</summary>
                <pre>{JSON.stringify(selected.config_schema, null, 2)}</pre>
              </details>
            </div>
          )}
        </div>
      )}
    </section>
  );
}
