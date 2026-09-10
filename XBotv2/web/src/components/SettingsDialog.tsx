import { Check, Clock3, FolderCog, Globe2, Layers3, Monitor, Moon, Palette, Server, Settings2, Sun, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { PermissionDecision, SandboxAccess, SessionPolicy, SessionPolicyPatch } from "../api/types";
import { PluginConfigPanel, type PluginConfigPanelProps } from "./PluginConfigPanel";

export type ThemePreference = "system" | "light" | "dark";
type SettingsScope = "global" | "workspace" | "session" | "temporary";

interface SettingsDialogProps {
  themePreference: ThemePreference;
  onThemeChange: (preference: ThemePreference) => void;
  sessionId?: string;
  threadId?: string;
  loadSessionPolicy?: (sessionId: string) => Promise<SessionPolicy>;
  updateSessionPolicy?: (sessionId: string, patch: SessionPolicyPatch) => Promise<SessionPolicy>;
  loadPluginConfig: PluginConfigPanelProps["load"];
  updatePluginConfig: PluginConfigPanelProps["update"];
  onClose: () => void;
}

const themeOptions: readonly {
  value: ThemePreference;
  label: string;
  description: string;
  icon: typeof Monitor;
}[] = [
  { value: "system", label: "Follow system", description: "Use your device appearance preference.", icon: Monitor },
  { value: "light", label: "Light", description: "Keep the interface bright and high contrast.", icon: Sun },
  { value: "dark", label: "Dark", description: "Use a low-glare dark interface.", icon: Moon },
];

export function SettingsDialog({ themePreference, onThemeChange, sessionId, threadId, loadSessionPolicy, updateSessionPolicy, loadPluginConfig, updatePluginConfig, onClose }: SettingsDialogProps) {
  const [scope, setScope] = useState<SettingsScope>("global");
  const closeButton = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKeyDown);
    closeButton.current?.focus();
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  return (
    <div className="settings-backdrop" role="presentation" onMouseDown={(event) => {
      if (event.currentTarget === event.target) onClose();
    }}>
      <section className="settings-dialog" role="dialog" aria-modal="true" aria-labelledby="settings-title">
        <nav className="settings-nav" aria-label="Settings sections">
          <div className="settings-nav-title"><Settings2 size={16} /> <span>Settings</span></div>
          <button type="button" className={scope === "global" ? "active" : ""} aria-current={scope === "global" ? "page" : undefined} onClick={() => setScope("global")}>
            <Globe2 size={15} /> Global
          </button>
          <button type="button" className={scope === "workspace" ? "active" : ""} aria-current={scope === "workspace" ? "page" : undefined} onClick={() => setScope("workspace")}>
            <FolderCog size={15} /> Workspace
          </button>
          <button type="button" className={scope === "session" ? "active" : ""} aria-current={scope === "session" ? "page" : undefined} onClick={() => setScope("session")}>
            <Layers3 size={15} /> Session
          </button>
          <button type="button" className={scope === "temporary" ? "active" : ""} aria-current={scope === "temporary" ? "page" : undefined} onClick={() => setScope("temporary")}>
            <Clock3 size={15} /> Temporary
          </button>
        </nav>

        <div className="settings-content">
          <header className="settings-header">
            <div>
              <span className="eyebrow">Preferences</span>
              <h2 id="settings-title">{scopeTitle(scope)}</h2>
            </div>
            <button ref={closeButton} type="button" className="icon-button" title="Close settings" aria-label="Close settings" onClick={onClose}>
              <X size={17} />
            </button>
          </header>

          {scope === "global" ? (
            <GlobalSettings
              themePreference={themePreference}
              onThemeChange={onThemeChange}
              sessionId={sessionId}
              threadId={threadId}
              loadPluginConfig={loadPluginConfig}
              updatePluginConfig={updatePluginConfig}
            />
          ) : scope === "workspace" ? (
            <WorkspaceSettings
              sessionId={sessionId}
              threadId={threadId}
              loadPluginConfig={loadPluginConfig}
              updatePluginConfig={updatePluginConfig}
            />
          ) : scope === "session" ? (
            <SessionSettings
              sessionId={sessionId}
              threadId={threadId}
              loadSessionPolicy={loadSessionPolicy}
              updateSessionPolicy={updateSessionPolicy}
              loadPluginConfig={loadPluginConfig}
              updatePluginConfig={updatePluginConfig}
            />
          ) : <TemporarySettings />}
        </div>
      </section>
    </div>
  );
}

function scopeTitle(scope: SettingsScope): string {
  return scope === "global"
    ? "Global settings"
    : scope === "workspace" ? "Workspace settings"
      : scope === "session" ? "Session settings"
        : "Temporary settings";
}

function GlobalSettings({
  themePreference,
  onThemeChange,
  sessionId,
  threadId,
  loadPluginConfig,
  updatePluginConfig,
}: Pick<SettingsDialogProps, "themePreference" | "onThemeChange" | "sessionId" | "threadId" | "loadPluginConfig" | "updatePluginConfig">) {
  return (
    <div className="settings-sections">
      <div className="settings-scope-intro">
        <span className="settings-scope-badge"><Globe2 size={14} /></span>
        <div><strong>Global</strong><p>Preferences and plugin defaults shared by new sessions.</p></div>
      </div>
      <ClientSettings themePreference={themePreference} onThemeChange={onThemeChange} />
      <PluginConfigPanel
        sessionId={sessionId}
        threadId={threadId}
        scope="global"
        load={loadPluginConfig}
        update={updatePluginConfig}
      />
    </div>
  );
}

function TemporarySettings() {
  return (
    <div className="settings-sections">
      <div className="settings-scope-intro">
        <span className="settings-scope-badge"><Clock3 size={14} /></span>
        <div><strong>Temporary</strong><p>Per-turn overrides will appear here in a future release.</p></div>
      </div>
      <section className="settings-empty" aria-label="Temporary settings unavailable">
        <Clock3 size={24} />
        <strong>No temporary settings yet</strong>
        <p>This scope is intentionally empty. Nothing here changes the saved global or session configuration.</p>
      </section>
    </div>
  );
}

function WorkspaceSettings({
  sessionId,
  threadId,
  loadPluginConfig,
  updatePluginConfig,
}: Pick<SettingsDialogProps, "sessionId" | "threadId" | "loadPluginConfig" | "updatePluginConfig">) {
  return (
    <div className="settings-sections">
      <div className="settings-scope-intro">
        <span className="settings-scope-badge"><FolderCog size={14} /></span>
        <div><strong>Workspace</strong><p>Plugin defaults shared by sessions in the active workspace.</p></div>
      </div>
      <PluginConfigPanel
        sessionId={sessionId}
        threadId={threadId}
        scope="workspace"
        load={loadPluginConfig}
        update={updatePluginConfig}
      />
    </div>
  );
}

function ClientSettings({ themePreference, onThemeChange }: Pick<SettingsDialogProps, "themePreference" | "onThemeChange">) {
  return (
    <>
      <section className="settings-section" aria-labelledby="appearance-title">
        <div className="settings-section-heading">
          <div>
            <h3 id="appearance-title">Appearance</h3>
            <p>Choose how XBot looks on this device. This preference is stored locally in your browser.</p>
          </div>
          <Palette size={18} aria-hidden="true" />
        </div>
        <div className="theme-options" role="radiogroup" aria-label="Theme preference">
          {themeOptions.map(({ value, label, description, icon: Icon }) => {
            const selected = value === themePreference;
            return (
              <button
                type="button"
                role="radio"
                aria-checked={selected}
                className={`theme-option ${selected ? "selected" : ""}`}
                key={value}
                onClick={() => onThemeChange(value)}
              >
                <span className="theme-option-icon"><Icon size={16} /></span>
                <span className="theme-option-copy"><strong>{label}</strong><small>{description}</small></span>
                {selected && <Check className="theme-option-check" size={15} aria-hidden="true" />}
              </button>
            );
          })}
        </div>
      </section>

      <section className="settings-section settings-note" aria-label="Client preference scope">
        <span className="settings-note-icon"><Settings2 size={16} /></span>
        <div>
          <strong>Only this browser</strong>
          <p>Client preferences do not change sessions, workspaces, providers, or server configuration.</p>
        </div>
      </section>
    </>
  );
}

function SessionSettings({
  sessionId,
  threadId,
  loadSessionPolicy,
  updateSessionPolicy,
  loadPluginConfig,
  updatePluginConfig,
}: {
  sessionId?: string;
  threadId?: string;
  loadSessionPolicy: SettingsDialogProps["loadSessionPolicy"];
  updateSessionPolicy: SettingsDialogProps["updateSessionPolicy"];
  loadPluginConfig: PluginConfigPanelProps["load"];
  updatePluginConfig: PluginConfigPanelProps["update"];
}) {
  return (
    <div className="settings-sections">
      <SessionPolicyPanel sessionId={sessionId} load={loadSessionPolicy} update={updateSessionPolicy} />
      <div className="settings-scope-intro">
        <span className="settings-scope-badge"><Layers3 size={14} /></span>
        <div><strong>Session</strong><p>Overrides for the active session. The editor uses each plugin's declared schema.</p></div>
      </div>
      <PluginConfigPanel
        sessionId={sessionId}
        threadId={threadId}
        scope="session"
        load={loadPluginConfig}
        update={updatePluginConfig}
      />
    </div>
  );
}

const sandboxFields = ["enabled", "network"] as const;
const accessFields = ["external_read", "external_write", "workspace_read", "workspace_write"] as const;
type SandboxSetting = "" | boolean | SandboxAccess;

function SessionPolicyPanel({
  sessionId,
  load,
  update,
}: {
  sessionId?: string;
  load?: (sessionId: string) => Promise<SessionPolicy>;
  update?: (sessionId: string, patch: SessionPolicyPatch) => Promise<SessionPolicy>;
}) {
  const [policy, setPolicy] = useState<SessionPolicy | null>(null);
  const [sandbox, setSandbox] = useState<Record<string, SandboxSetting>>({});
  const [tool, setTool] = useState("");
  const [decision, setDecision] = useState<PermissionDecision | "inherit">("ask");
  const [status, setStatus] = useState("loading");

  useEffect(() => {
    if (!sessionId || !load) {
      setPolicy(null);
      setStatus(sessionId ? "Server policy unavailable." : "Open a session to edit its server policy.");
      return;
    }
    let active = true;
    setStatus("Loading session policy…");
    void load(sessionId).then((value) => {
      if (!active) return;
      setPolicy(value);
      setSandbox(policySandboxOverrides(value));
      setStatus("ready");
    }).catch((error) => {
      if (active) setStatus(error instanceof Error ? error.message : String(error));
    });
    return () => { active = false; };
  }, [load, sessionId]);

  const save = async () => {
    if (!sessionId || !update) return;
    setStatus("saving");
    try {
      const baseline = policy?.sandbox || {};
      const sandboxPatch: Record<string, boolean | SandboxAccess> = {};
      const removeSandbox: string[] = [];
      for (const [key, value] of Object.entries(sandbox)) {
        if (value === "") {
          if (Object.hasOwn(baseline, key)) removeSandbox.push(key);
        } else if (baseline[key] !== value) sandboxPatch[key] = value;
      }
      const permissionPatch = tool.trim()
        ? decision === "inherit"
          ? { remove_permissions: [tool.trim()] }
          : { permissions: { [tool.trim()]: decision } }
        : {};
      const updated = await update(sessionId, {
        ...(Object.keys(sandboxPatch).length ? { sandbox: sandboxPatch } : {}),
        ...(removeSandbox.length ? { remove_sandbox: removeSandbox } : {}),
        ...permissionPatch,
      });
      setPolicy(updated);
      setSandbox(policySandboxOverrides(updated));
      setTool("");
      setStatus("saved");
    } catch (error) {
      setStatus(error instanceof Error ? error.message : String(error));
    }
  };

  if (!load || !update) return null;

  return (
    <section className="settings-section" aria-labelledby="server-policy-title">
      <div className="settings-section-heading">
        <div>
          <h3 id="server-policy-title">Session security policy</h3>
          <p>Sandbox and tool permissions are stored by the server for this session.</p>
        </div>
        <Server size={18} aria-hidden="true" />
      </div>
      {!policy && <p className="settings-save-status" role="status">{status}</p>}
      {policy && <div className="server-settings-form">
        {sandboxFields.map((field) => <label key={field}>
          <span>{field === "enabled" ? "Sandbox enabled" : "Network access"}</span>
          <select value={String(sandbox[field] ?? "")} onChange={(event) => setSandbox({ ...sandbox, [field]: booleanSetting(event.target.value) })}>
            <option value="">inherit ({Boolean(policy.effective_sandbox[field]) ? "enabled" : "disabled"})</option>
            <option value="true">enabled</option><option value="false">disabled</option>
          </select>
        </label>)}
        {accessFields.map((field) => <label key={field}>
          <span>{policyFieldLabel(field)}</span>
          <select value={String(sandbox[field] ?? "")} onChange={(event) => setSandbox({ ...sandbox, [field]: event.target.value as SandboxSetting })}>
            <option value="">inherit ({sandboxAccess(policy.effective_sandbox[field])})</option>
            {(["allow", "readonly", "readwrite", "deny"] as const).map((value) => <option key={value} value={value}>{value}</option>)}
          </select>
        </label>)}
        <div className="permission-rule-editor">
          <input value={tool} onChange={(event) => setTool(event.target.value)} placeholder="Exact Tool name (optional)" aria-label="Permission Tool name" />
          <select value={decision} onChange={(event) => setDecision(event.target.value as PermissionDecision | "inherit")} aria-label="Permission decision">
            <option value="ask">ask</option><option value="allow">allow</option><option value="deny">deny</option><option value="inherit">inherit (remove)</option>
          </select>
        </div>
        <button type="button" className="primary-button" disabled={status === "saving"} onClick={() => void save()}>{status === "saving" ? "Saving…" : "Save server policy"}</button>
        <small className="settings-save-status" role="status">{status === "saved" ? "Saved" : status !== "ready" && status !== "saving" ? status : ""}</small>
      </div>}
    </section>
  );
}

function policySandboxOverrides(policy: SessionPolicy): Record<string, SandboxSetting> {
  return Object.fromEntries([...sandboxFields, ...accessFields].map((field) => {
    const value = policy.sandbox[field];
    return [field, typeof value === "boolean" || typeof value === "string" ? value as SandboxSetting : ""];
  }));
}

function booleanSetting(value: string): "" | boolean {
  return value === "" ? "" : value === "true";
}

function sandboxAccess(value: unknown): SandboxAccess {
  return value === "allow" || value === "deny" || value === "readonly" || value === "readwrite" ? value : "deny";
}

function policyFieldLabel(field: string): string {
  return field.split("_").map((part) => part.charAt(0).toUpperCase() + part.slice(1)).join(" ");
}
