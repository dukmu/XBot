import { Check, Clock3, FolderCog, Globe2, Layers3, Monitor, Moon, Palette, PanelLeftClose, PanelLeftOpen, Settings2, Sun, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { PluginConfigPanel, type PluginConfigPanelProps } from "./PluginConfigPanel";

export type ThemePreference = "system" | "light" | "dark";
type SettingsScope = "global" | "workspace" | "session" | "temporary";

interface SettingsDialogProps {
  themePreference: ThemePreference;
  onThemeChange: (preference: ThemePreference) => void;
  sessionId?: string;
  threadId?: string;
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

const NAV_STORAGE_KEY = "xbotv2.settingsNav";

function readNavOpen(): boolean {
  try {
    return window.localStorage.getItem(NAV_STORAGE_KEY) !== "collapsed";
  } catch {
    return true;
  }
}

export function SettingsDialog({ themePreference, onThemeChange, sessionId, threadId, loadPluginConfig, updatePluginConfig, onClose }: SettingsDialogProps) {
  const [scope, setScope] = useState<SettingsScope>("global");
  const [navOpen, setNavOpen] = useState(readNavOpen);
  const closeButton = useRef<HTMLButtonElement>(null);

  const toggleNav = () => {
    setNavOpen((open) => {
      try {
        window.localStorage.setItem(NAV_STORAGE_KEY, open ? "collapsed" : "open");
      } catch {
        // A blocked storage area only costs the remembered preference.
      }
      return !open;
    });
  };

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
        <nav className={`settings-nav${navOpen ? "" : " collapsed"}`} aria-label="Settings sections">
          <div className="settings-nav-title">
            <Settings2 className="settings-nav-mark" size={16} aria-hidden="true" />
            <span>Settings</span>
            <button
              type="button"
              className="settings-nav-toggle"
              aria-expanded={navOpen}
              aria-label={navOpen ? "Collapse settings navigation" : "Expand settings navigation"}
              title={navOpen ? "Collapse navigation" : "Expand navigation"}
              onClick={toggleNav}
            >
              {navOpen ? <PanelLeftClose size={14} aria-hidden="true" /> : <PanelLeftOpen size={14} aria-hidden="true" />}
            </button>
          </div>
          {scopeOptions.map(({ value, label, icon: Icon }) => (
            <button
              key={value}
              type="button"
              className={scope === value ? "active" : ""}
              aria-current={scope === value ? "page" : undefined}
              aria-label={label}
              title={navOpen ? undefined : label}
              onClick={() => setScope(value)}
            >
              <Icon size={15} aria-hidden="true" /> <span>{label}</span>
            </button>
          ))}
        </nav>

        <div className="settings-content">
          <header className="settings-header">
            <div>
              <h2 id="settings-title">{scopeTitle(scope)}</h2>
              <p className="settings-subtitle">{scopeDescription(scope)}</p>
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
              loadPluginConfig={loadPluginConfig}
              updatePluginConfig={updatePluginConfig}
            />
          ) : <TemporarySettings />}
        </div>
      </section>
    </div>
  );
}

const scopeOptions: readonly { value: SettingsScope; label: string; icon: typeof Globe2 }[] = [
  { value: "global", label: "Global", icon: Globe2 },
  { value: "workspace", label: "Workspace", icon: FolderCog },
  { value: "session", label: "Session", icon: Layers3 },
  { value: "temporary", label: "Temporary", icon: Clock3 },
];

function scopeDescription(scope: SettingsScope): string {
  return scope === "global"
    ? "Defaults for new sessions and this browser."
    : scope === "workspace"
      ? "Defaults for sessions in the active workspace."
      : scope === "session"
        ? "Overrides for the active session."
        : "Per-turn overrides, not saved anywhere.";
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
  loadPluginConfig,
  updatePluginConfig,
}: Pick<SettingsDialogProps, "sessionId" | "threadId" | "loadPluginConfig" | "updatePluginConfig">) {
  return (
    <div className="settings-sections">
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
