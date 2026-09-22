import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import {
  AccessModeButton,
  FULL_ACCESS_CONFIRM,
  displayAccessPreset,
  patchForPreset,
  presetFromPolicy,
} from "./AccessModeButton";
import type { SessionPolicy } from "../api/types";

const policy = (sandbox: Record<string, unknown>): SessionPolicy => ({
  session_id: "s",
  permissions: {},
  effective_permissions: {},
  sandbox,
  effective_sandbox: sandbox,
});

describe("access mode presets", () => {
  it("title-cases host presets and gives Full access its product label", () => {
    // Ported naming rule from dsh's permission presets.
    expect(displayAccessPreset("workspace-write")).toBe("Workspace Write");
    expect(displayAccessPreset("readonly")).toBe("Readonly");
    expect(displayAccessPreset("danger-full-access")).toBe("Full access");
  });

  it("reads the current preset from the effective sandbox", () => {
    expect(presetFromPolicy(null)).toBe("readonly");
    expect(presetFromPolicy(policy({ workspace_write: "allow", external_write: "deny" })))
      .toBe("workspace-write");
    expect(presetFromPolicy(policy({ workspace_write: "allow", external_write: "allow" })))
      .toBe("danger-full-access");
    expect(presetFromPolicy(policy({ workspace_write: "readonly", external_write: "deny" })))
      .toBe("readonly");
  });

  it("applies the matching sandbox patch", () => {
    expect(patchForPreset("workspace-write").sandbox).toMatchObject({
      workspace_write: "allow",
      external_write: "deny",
    });
    expect(patchForPreset("danger-full-access").sandbox).toMatchObject({
      external_write: "allow",
    });
    expect(patchForPreset("readonly").sandbox).toMatchObject({
      workspace_write: "readonly",
      network: false,
    });
  });
});

describe("AccessModeButton", () => {
  it("announces the current mode the way dsh does", () => {
    const { getByLabelText } = render(
      <AccessModeButton
        policy={policy({ workspace_write: "allow", external_write: "deny" })}
        onSelect={async () => undefined}
      />,
    );
    expect(getByLabelText("Access mode, current: Workspace Write")).toBeTruthy();
  });

  it("applies a low-risk preset immediately", async () => {
    const onSelect = vi.fn(async () => undefined);
    const { getByLabelText, getByText } = render(
      <AccessModeButton policy={policy({ workspace_write: "allow" })} onSelect={onSelect} />,
    );
    fireEvent.click(getByLabelText(/^Access mode/));
    fireEvent.click(getByText("Readonly"));
    expect(onSelect).toHaveBeenCalledWith("readonly");
  });

  it("gates Full access behind the ported risk confirmation", async () => {
    const onSelect = vi.fn(async () => undefined);
    const { getByLabelText, getByText, getByRole } = render(
      <AccessModeButton policy={policy({ workspace_write: "allow" })} onSelect={onSelect} />,
    );
    fireEvent.click(getByLabelText(/^Access mode/));
    fireEvent.click(getByText("Full access"));

    // Nothing applied yet: the dialog is up and its action waits for the
    // acknowledgement, as the ported confirmation does.
    expect(onSelect).not.toHaveBeenCalled();
    const dialog = getByRole("dialog", { name: FULL_ACCESS_CONFIRM.title });
    expect(dialog.textContent).toContain(FULL_ACCESS_CONFIRM.description);
    const enable = getByRole("button", { name: FULL_ACCESS_CONFIRM.enable });
    expect(enable).toHaveProperty("disabled", true);

    fireEvent.click(getByRole("checkbox", { name: FULL_ACCESS_CONFIRM.acknowledge }));
    expect(enable).toHaveProperty("disabled", false);
    fireEvent.click(enable);
    expect(onSelect).toHaveBeenCalledWith("danger-full-access");
    expect(screen.queryByRole("dialog", { name: FULL_ACCESS_CONFIRM.title })).toBeNull();
  });

  it("cancels the risk confirmation without applying anything", () => {
    const onSelect = vi.fn(async () => undefined);
    const { getByLabelText, getByText, getByRole } = render(
      <AccessModeButton policy={policy({ workspace_write: "allow" })} onSelect={onSelect} />,
    );
    fireEvent.click(getByLabelText(/^Access mode/));
    fireEvent.click(getByText("Full access"));
    fireEvent.click(getByRole("button", { name: FULL_ACCESS_CONFIRM.cancel }));
    expect(onSelect).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog", { name: FULL_ACCESS_CONFIRM.title })).toBeNull();
  });
});
