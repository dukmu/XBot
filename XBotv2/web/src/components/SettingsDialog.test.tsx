import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { SettingsDialog } from "./SettingsDialog";

describe("SettingsDialog", () => {
  it("switches between client and server sections", () => {
    render(<SettingsDialog themePreference="system" onThemeChange={vi.fn()} onClose={vi.fn()} />);

    expect(screen.getByRole("heading", { name: "Client settings" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: /Server/ }));
    expect(screen.getByRole("heading", { name: "Server settings" })).toBeVisible();
    expect(screen.getByText("Read-only preview")).toBeVisible();
  });

  it("reports theme changes and closes on Escape", () => {
    const onThemeChange = vi.fn();
    const onClose = vi.fn();
    render(<SettingsDialog themePreference="system" onThemeChange={onThemeChange} onClose={onClose} />);

    fireEvent.click(screen.getByRole("radio", { name: /Dark/ }));
    expect(onThemeChange).toHaveBeenCalledWith("dark");
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
