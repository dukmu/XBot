import { Check, Monitor, Moon, Palette, Server, Settings2, Sun, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { PermissionDecision, SandboxAccess, SessionPolicy, SessionPolicyPatch } from "../api/types";
import { PluginConfigPanel, type PluginConfigPanelProps } from "./PluginConfigPanel";

export type ThemePreference = "system" | "light" | "dark";
type SettingsSection = "client" | "server";

interface SettingsDialogProps {
  themePreference: ThemePreference;
  onThemeChange: (preference: ThemePreference) => void;
  sessionId?: string;
  threadId?: string;
  loadSessionPolicy: (sessionId: string) => Promise<SessionPolicy>;
  updateSessionPolicy: (sessionId: string, patch: SessionPolicyPatch) => Promise<SessionPolicy>;
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
  const [section, setSection] = useState<SettingsSection>("client");
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
          <button
            type="button"
            className={section === "client" ? "active" : ""}
            aria-current={section === "client" ? "page" : undefined}
            onClick={() => setSection("client")}
          >
            <Palette size={15} /> Client
          </button>
          <button
            type="button"
            className={section === "server" ? "active" : ""}
            aria-current={section === "server" ? "page" : undefined}
            onClick={() => setSection("server")}
          >
            <Server size={15} /> Server
          </button>
        </nav>

        <div className="settings-content">
          <header className="settings-header">
            <div>
              <span className="eyebrow">Preferences</span>
              <h2 id="settings-title">{section === "client" ? "Client settings" : "Server settings"}</h2>
            </div>
            <button ref={closeButton} type="button" className="icon-button" title="Close settings" aria-label="Close settings" onClick={onClose}>
              <X size={17} />
            </button>
          </header>

          {section === "client" ? (
            <ClientSettings themePreference={themePreference} onThemeChange={onThemeChange} />
          ) : (
            <ServerSettings
              sessionId={sessionId}
              threadId={threadId}
              loadPolicy={loadSessionPolicy}
              updatePolicy={updateSessionPolicy}
              loadPluginConfig={loadPluginConfig}
              updatePluginConfig={updatePluginConfig}
            />
          )}
        </div>
      </section>
    </div>
  );
}

function ClientSettings({ themePreference, onThemeChange }: Pick<SettingsDialogProps, "themePreference" | "onThemeChange">) {
  return (
    <div className="settings-sections">
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
    </div>
  );
}

const accessFields = ["external_read", "external_write", "workspace_read", "workspace_write"] as const;
const booleanFields = ["enabled", "network"] as const;
type SandboxSetting = "" | boolean | SandboxAccess;

function ServerSettings({
  sessionId,
  threadId,
  loadPolicy,
  updatePolicy,
  loadPluginConfig,
  updatePluginConfig,
}: {
  sessionId?: string;
  threadId?: string;
  loadPolicy: (sessionId: string) => Promise<SessionPolicy>;
  updatePolicy: (sessionId: string, patch: SessionPolicyPatch) => Promise<SessionPolicy>;
  loadPluginConfig: PluginConfigPanelProps["load"];
  updatePluginConfig: PluginConfigPanelProps["update"];
}) {
  const [policy, setPolicy] = useState<SessionPolicy | null>(null);
  const [sandbox, setSandbox] = useState<Record<string, SandboxSetting>>({});
  const [tool, setTool] = useState("");
  const [decision, setDecision] = useState<PermissionDecision | "inherit">("ask");
  const [status, setStatus] = useState("loading");

  useEffect(() => {
    if (!sessionId) {
      setStatus("idle");
      return;
    }
    let active = true;
    setStatus("loading");
    void loadPolicy(sessionId).then((value) => {
      if (!active) return;
      setPolicy(value);
      setSandbox(policySandboxOverrides(value));
      setStatus("ready");
    }).catch((error) => {
      if (active) setStatus(error instanceof Error ? error.message : String(error));
    });
    return () => { active = false; };
  }, [loadPolicy, sessionId]);

  const save = async () => {
    if (!sessionId) return;
    setStatus("saving");
    try {
      const baseline = policy?.sandbox || {};
      const sandboxPatch: Record<string, boolean | SandboxAccess> = {};
      const removeSandbox: string[] = [];
      for (const [key, value] of Object.entries(sandbox)) {
        if (value === "") {
          if (Object.hasOwn(baseline, key)) removeSandbox.push(key);
        } else if (baseline[key] !== value) {
          sandboxPatch[key] = value;
        }
      }
      const permissionPatch = tool.trim()
        ? decision === "inherit"
          ? { remove_permissions: [tool.trim()] }
          : { permissions: { [tool.trim()]: decision } }
        : {};
      const updated = await updatePolicy(sessionId, {
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

  if (!sessionId) {
    return <div className="settings-sections"><section className="settings-section settings-note"><p>Open a session to edit its server policy.</p></section></div>;
  }

  return (
    <div className="settings-sections">
      <section className="settings-section" aria-labelledby="server-policy-title">
        <div className="settings-section-heading">
          <div>
            <h3 id="server-policy-title">Session security policy</h3>
            <p>Changes are stored by the server for session <code>{sessionId}</code>.</p>
          </div>
          <Server size={18} aria-hidden="true" />
        </div>
        {!policy && <p className="settings-save-status" role="status">
          {status === "loading" ? "Loading session policy…" : status}
        </p>}
        {policy && <div className="server-settings-form">
          {booleanFields.map((field) => <label key={field}>
            <span>{field === "enabled" ? "Sandbox enabled" : "Network access"}</span>
            <select value={String(sandbox[field] ?? "")} onChange={(event) => setSandbox({ ...sandbox, [field]: booleanSetting(event.target.value) })}>
              <option value="">inherit ({Boolean(policy.effective_sandbox[field]) ? "enabled" : "disabled"})</option>
              <option value="true">enabled</option>
              <option value="false">disabled</option>
            </select>
          </label>)}
          {accessFields.map((field) => <label key={field}>
            <span>{field.replaceAll("_", " ")}</span>
            <select value={String(sandbox[field] ?? "")} onChange={(event) => setSandbox({ ...sandbox, [field]: event.target.value as "" | SandboxAccess })}>
              <option value="">inherit ({sandboxAccess(policy.effective_sandbox[field], "deny")})</option>
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
        </div>
        }
      </section>
      <section className="settings-section settings-note" aria-label="Server settings availability">
        <span className="settings-note-icon"><Server size={16} /></span>
        <div>
          <strong>Session policy only</strong>
          <p>Session policy is edited here. Plugin configuration below is schema-driven and revisioned; changes apply to new sessions and unavailable schemas remain read-only.</p>
        </div>
      </section>
      <PluginConfigPanel
        sessionId={sessionId}
        threadId={threadId}
        load={loadPluginConfig}
        update={updatePluginConfig}
      />
    </div>
  );
}

function policySandboxOverrides(policy: SessionPolicy): Record<string, SandboxSetting> {
  return Object.fromEntries([...booleanFields, ...accessFields].map((field) => {
    const value = policy.sandbox[field];
    return [field, typeof value === "boolean" || typeof value === "string" ? value as SandboxSetting : ""];
  }));
}

function booleanSetting(value: string): "" | boolean {
  return value === "" ? "" : value === "true";
}

function sandboxAccess(value: unknown, fallback: SandboxAccess): SandboxAccess {
  return value === "allow" || value === "deny" || value === "readonly" || value === "readwrite" ? value : fallback;
}
