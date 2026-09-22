/* Preset model ported from DeepSeek Harness ui-permission-presets (MIT): the
 * host supplies kebab-case presets, the UI title-cases them, and Full access
 * sits behind an explicit confirmation. */
import { ShieldCheck, TriangleAlert, X } from "lucide-react";
import { useMemo, useState } from "react";
import type { SessionPolicy, SessionPolicyPatch } from "../api/types";

export type AccessPreset = "readonly" | "workspace-write" | "danger-full-access";

const FULL_ACCESS_PRESET: AccessPreset = "danger-full-access";

export const ACCESS_PRESETS: AccessPreset[] = [
  "readonly",
  "workspace-write",
  "danger-full-access",
];

/** ``danger-full-access`` has a product label of its own. */
export function displayAccessPreset(preset: AccessPreset): string {
  return preset === FULL_ACCESS_PRESET
    ? "Full access"
    : preset.split("-").map((word) => word.charAt(0).toUpperCase() + word.slice(1)).join(" ");
}

/** Confirmation copy, taken from the same source as the preset names. */
export const FULL_ACCESS_CONFIRM = {
  title: "Enable Full access?",
  description:
    "Full access reduces confirmation steps and lets the agent perform more actions directly, "
    + "including sensitive operations, file changes, or external commands. Only use it when you "
    + "trust the current task.",
  enable: "Enable Full access",
  /** Ported acknowledgement gate: the primary action waits for this. */
  acknowledge: "I understand the risks and want to continue",
  cancel: "Cancel",
  close: "Close",
};


type Sandbox = Record<string, unknown>;

function sandboxOf(policy: SessionPolicy | null): Sandbox {
  if (policy === null) return {};
  const effective = policy.effective_sandbox;
  return effective && typeof effective === "object" ? effective as Sandbox : {};
}

function allows(value: unknown): boolean {
  return value === "allow" || value === "readwrite" || value === true;
}

/** Which preset the session's effective sandbox currently corresponds to. */
export function presetFromPolicy(policy: SessionPolicy | null): AccessPreset {
  const sandbox = sandboxOf(policy);
  if (allows(sandbox.external_write)) return "danger-full-access";
  if (allows(sandbox.workspace_write)) return "workspace-write";
  return "readonly";
}

/** The policy patch a preset applies. */
export function patchForPreset(preset: AccessPreset): SessionPolicyPatch {
  switch (preset) {
    case "danger-full-access":
      return { sandbox: { workspace_write: "allow", external_write: "allow", network: true } };
    case "workspace-write":
      return { sandbox: { workspace_write: "allow", external_write: "deny", network: true } };
    default:
      return { sandbox: { workspace_write: "readonly", external_write: "deny", network: false } };
  }
}

export function AccessModeButton({
  policy,
  disabled,
  onSelect,
}: {
  policy: SessionPolicy | null;
  disabled?: boolean;
  onSelect: (preset: AccessPreset) => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [acknowledged, setAcknowledged] = useState(false);
  const current = useMemo(() => presetFromPolicy(policy), [policy]);
  const label = displayAccessPreset(current);

  const choose = async (preset: AccessPreset) => {
    if (preset === FULL_ACCESS_PRESET && current !== FULL_ACCESS_PRESET) {
      // The ported gate is a dialog of its own, so the menu closes under it.
      setOpen(false);
      setAcknowledged(false);
      setConfirming(true);
      return;
    }
    setOpen(false);
    await onSelect(preset);
  };

  return (
    <div className="access-mode">
      <button
        type="button"
        className="composer-tool access-mode-button"
        aria-label={`Access mode, current: ${label}`}
        title={`Access mode, current: ${label}`}
        disabled={disabled}
        onClick={() => {
          setConfirming(false);
          setOpen((value) => !value);
        }}
      >
        <ShieldCheck size={14} />
        <span className="access-mode-label">{label}</span>
      </button>
      {open && (
        <div className="access-mode-menu" role="menu" aria-label="Access mode">
          {ACCESS_PRESETS.map((preset) => (
            <button
              key={preset}
              type="button"
              role="menuitemradio"
              aria-checked={preset === current}
              className={`access-mode-option${preset === current ? " is-current" : ""}`}
              data-preset={preset}
              onClick={() => void choose(preset)}
            >
              {displayAccessPreset(preset)}
            </button>
          ))}
        </div>
      )}
      {confirming && (
        // Ported risk confirmation: a dialog whose primary action stays
        // unavailable until the acknowledgement is checked.
        <div className="dialog-backdrop" role="presentation" onMouseDown={(event) => {
          if (event.currentTarget === event.target) setConfirming(false);
        }}>
          <section
            className="dialog access-confirm-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="access-confirm-title"
            onKeyDown={(event) => {
              if (event.key === "Escape") setConfirming(false);
            }}
          >
            <header className="access-confirm-head">
              <h2 id="access-confirm-title">{FULL_ACCESS_CONFIRM.title}</h2>
              <button
                type="button"
                className="icon-button"
                aria-label={FULL_ACCESS_CONFIRM.close}
                title={FULL_ACCESS_CONFIRM.close}
                onClick={() => setConfirming(false)}
              >
                <X size={15} />
              </button>
            </header>
            <div className="access-confirm-warning">
              <TriangleAlert size={18} aria-hidden />
              <p>{FULL_ACCESS_CONFIRM.description}</p>
            </div>
            <label className="access-confirm-acknowledge">
              <input
                type="checkbox"
                autoFocus
                checked={acknowledged}
                onChange={(event) => setAcknowledged(event.currentTarget.checked)}
              />
              <span>{FULL_ACCESS_CONFIRM.acknowledge}</span>
            </label>
            <div className="access-confirm-actions">
              <button type="button" className="secondary-button" onClick={() => setConfirming(false)}>
                {FULL_ACCESS_CONFIRM.cancel}
              </button>
              <button
                type="button"
                className="primary-button"
                disabled={!acknowledged}
                onClick={() => {
                  setConfirming(false);
                  setAcknowledged(false);
                  void onSelect(FULL_ACCESS_PRESET);
                }}
              >
                {FULL_ACCESS_CONFIRM.enable}
              </button>
            </div>
          </section>
        </div>
      )}
    </div>
  );
}
