import { Check, Monitor, Moon, Palette, Server, Settings2, Sun, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";

export type ThemePreference = "system" | "light" | "dark";
type SettingsSection = "client" | "server";

interface SettingsDialogProps {
  themePreference: ThemePreference;
  onThemeChange: (preference: ThemePreference) => void;
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

export function SettingsDialog({ themePreference, onThemeChange, onClose }: SettingsDialogProps) {
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
            <ServerSettings />
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

function ServerSettings() {
  return (
    <div className="settings-sections">
      <section className="settings-section" aria-labelledby="server-preview-title">
        <div className="settings-section-heading">
          <div>
            <h3 id="server-preview-title">Server configuration</h3>
            <p>These controls are reserved for server-backed settings. No configuration API is called by this preview.</p>
          </div>
          <Server size={18} aria-hidden="true" />
        </div>
        <div className="server-settings-preview">
          <div><span>Connection</span><strong>Current XBot server</strong></div>
          <div><span>Endpoint</span><code>{window.location.origin}</code></div>
          <div><span>Status</span><strong className="server-settings-readonly">Read-only preview</strong></div>
        </div>
      </section>
      <section className="settings-section settings-note" aria-label="Server settings availability">
        <span className="settings-note-icon"><Server size={16} /></span>
        <div>
          <strong>Server settings are not connected yet</strong>
          <p>Provider credentials, plugins, permissions, and defaults remain managed by the server until their public API is ready.</p>
        </div>
      </section>
    </div>
  );
}
