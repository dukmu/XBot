import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { SettingsDialog } from "./SettingsDialog";

describe("SettingsDialog", () => {
  it("switches between global, session, and temporary scopes", () => {
    render(<SettingsDialog themePreference="system" onThemeChange={vi.fn()} loadPluginConfig={vi.fn()} updatePluginConfig={vi.fn()} onClose={vi.fn()} />);

    expect(screen.getByRole("heading", { name: "Global settings" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: /Workspace/ }));
    expect(screen.getByRole("heading", { name: "Workspace settings" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: /Session/ }));
    expect(screen.getByRole("heading", { name: "Session settings" })).toBeVisible();
    expect(screen.getByText("Open a session to edit plugin configuration.")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: /Temporary/ }));
    expect(screen.getByRole("heading", { name: "Temporary settings" })).toBeVisible();
    expect(screen.getByText("No temporary settings yet")).toBeVisible();
  });

  it("reports theme changes and closes on Escape", () => {
    const onThemeChange = vi.fn();
    const onClose = vi.fn();
    render(<SettingsDialog themePreference="system" onThemeChange={onThemeChange} loadPluginConfig={vi.fn()} updatePluginConfig={vi.fn()} onClose={onClose} />);

    fireEvent.click(screen.getByRole("radio", { name: /Dark/ }));
    expect(onThemeChange).toHaveBeenCalledWith("dark");
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("loads and persists the active session plugin layer", async () => {
    const catalog = { scope: "session" as const, workspace_root: "/workspace", revision: "r", applies_to: "new_sessions" as const, plugins: [{ plugin_id: "compact", name: "compact", editable: true, config_schema: { type: "object", properties: { automatic: { type: "boolean" } } }, scope_config: { automatic: true }, effective_config: { automatic: true }, unavailable_reason: "" }] };
    const loadPluginConfig = vi.fn().mockResolvedValue(catalog);
    const updatePluginConfig = vi.fn().mockResolvedValue({ ...catalog, revision: "r2" });
    render(<SettingsDialog themePreference="system" onThemeChange={vi.fn()} sessionId="s1" threadId="t1" loadPluginConfig={loadPluginConfig} updatePluginConfig={updatePluginConfig} onClose={vi.fn()} />);

    fireEvent.click(screen.getByRole("button", { name: /Session/ }));
    const save = await screen.findByRole("button", { name: "Save plugin configuration" });
    expect(save).toBeDisabled();
    fireEvent.click(screen.getByLabelText("Automatic"));
    fireEvent.click(screen.getByRole("button", { name: "Save plugin configuration" }));

    await waitFor(() => expect(updatePluginConfig).toHaveBeenCalledWith("s1", "t1", "compact", "session", "r", expect.any(Object)));
  });

  it("shows a plugin configuration loading failure", async () => {
    render(<SettingsDialog
      themePreference="system"
      onThemeChange={vi.fn()}
      sessionId="s1"
      threadId="t1"
      loadPluginConfig={vi.fn().mockRejectedValue(new Error("plugin configuration unavailable"))}
      updatePluginConfig={vi.fn()}
      onClose={vi.fn()}
    />);

    fireEvent.click(screen.getByRole("button", { name: /Session/ }));
    expect(await screen.findByText("plugin configuration unavailable")).toBeVisible();
  });
});
