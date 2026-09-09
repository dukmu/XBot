import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { SettingsDialog } from "./SettingsDialog";

describe("SettingsDialog", () => {
  it("switches between client and server sections", () => {
    render(<SettingsDialog themePreference="system" onThemeChange={vi.fn()} loadSessionPolicy={vi.fn()} updateSessionPolicy={vi.fn()} loadPluginConfig={vi.fn()} updatePluginConfig={vi.fn()} onClose={vi.fn()} />);

    expect(screen.getByRole("heading", { name: "Client settings" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: /Server/ }));
    expect(screen.getByRole("heading", { name: "Server settings" })).toBeVisible();
    expect(screen.getByText("Open a session to edit its server policy.")).toBeVisible();
  });

  it("reports theme changes and closes on Escape", () => {
    const onThemeChange = vi.fn();
    const onClose = vi.fn();
    render(<SettingsDialog themePreference="system" onThemeChange={onThemeChange} loadSessionPolicy={vi.fn()} updateSessionPolicy={vi.fn()} loadPluginConfig={vi.fn()} updatePluginConfig={vi.fn()} onClose={onClose} />);

    fireEvent.click(screen.getByRole("radio", { name: /Dark/ }));
    expect(onThemeChange).toHaveBeenCalledWith("dark");
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("loads and persists the active session policy", async () => {
    const policy = {
      session_id: "s1", permissions: {}, effective_permissions: {}, sandbox: {},
      effective_sandbox: { enabled: true, network: true, external_read: "readonly", external_write: "deny", workspace_read: "allow", workspace_write: "allow" },
    };
    const loadSessionPolicy = vi.fn().mockResolvedValue(policy);
    const updateSessionPolicy = vi.fn().mockResolvedValue(policy);
    render(<SettingsDialog themePreference="system" onThemeChange={vi.fn()} sessionId="s1" loadSessionPolicy={loadSessionPolicy} updateSessionPolicy={updateSessionPolicy} loadPluginConfig={vi.fn().mockResolvedValue({ scope: "workspace", workspace_root: "/workspace", revision: "r", applies_to: "new_sessions", plugins: [] })} updatePluginConfig={vi.fn()} onClose={vi.fn()} />);

    fireEvent.click(screen.getByRole("button", { name: /Server/ }));
    await screen.findByRole("button", { name: "Save server policy" });
    fireEvent.change(screen.getByLabelText("Network access"), { target: { value: "false" } });
    fireEvent.change(screen.getByLabelText("Permission Tool name"), { target: { value: "shell" } });
    fireEvent.change(screen.getByLabelText("Permission decision"), { target: { value: "allow" } });
    fireEvent.click(screen.getByRole("button", { name: "Save server policy" }));

    await waitFor(() => expect(updateSessionPolicy).toHaveBeenCalledWith("s1", expect.objectContaining({
      permissions: { shell: "allow" },
      sandbox: expect.objectContaining({ network: false }),
    })));
  });

  it("can remove session overrides and return to inherited policy", async () => {
    const policy = {
      session_id: "s1", permissions: {}, effective_permissions: {},
      sandbox: { network: false },
      effective_sandbox: { enabled: true, network: false, external_read: "readonly", external_write: "deny", workspace_read: "allow", workspace_write: "allow" },
    };
    const loadSessionPolicy = vi.fn().mockResolvedValue(policy);
    const updateSessionPolicy = vi.fn().mockResolvedValue({ ...policy, sandbox: {} });
    render(<SettingsDialog themePreference="system" onThemeChange={vi.fn()} sessionId="s1" loadSessionPolicy={loadSessionPolicy} updateSessionPolicy={updateSessionPolicy} loadPluginConfig={vi.fn().mockResolvedValue({ scope: "workspace", workspace_root: "/workspace", revision: "r", applies_to: "new_sessions", plugins: [] })} updatePluginConfig={vi.fn()} onClose={vi.fn()} />);

    fireEvent.click(screen.getByRole("button", { name: /Server/ }));
    await screen.findByRole("button", { name: "Save server policy" });
    fireEvent.change(screen.getByLabelText("Network access"), { target: { value: "" } });
    fireEvent.change(screen.getByLabelText("Permission Tool name"), { target: { value: "shell" } });
    fireEvent.change(screen.getByLabelText("Permission decision"), { target: { value: "inherit" } });
    fireEvent.click(screen.getByRole("button", { name: "Save server policy" }));

    await waitFor(() => expect(updateSessionPolicy).toHaveBeenCalledWith("s1", {
      remove_permissions: ["shell"],
      remove_sandbox: ["network"],
    }));
  });

  it("shows a server policy loading failure", async () => {
    render(<SettingsDialog
      themePreference="system"
      onThemeChange={vi.fn()}
      sessionId="s1"
      loadSessionPolicy={vi.fn().mockRejectedValue(new Error("policy unavailable"))}
      updateSessionPolicy={vi.fn()}
      loadPluginConfig={vi.fn().mockResolvedValue({ scope: "workspace", workspace_root: "/workspace", revision: "r", applies_to: "new_sessions", plugins: [] })}
      updatePluginConfig={vi.fn()}
      onClose={vi.fn()}
    />);

    fireEvent.click(screen.getByRole("button", { name: /Server/ }));
    expect(await screen.findByRole("status")).toHaveTextContent("policy unavailable");
  });
});
