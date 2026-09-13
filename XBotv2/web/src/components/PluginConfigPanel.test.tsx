import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { PluginConfigCatalog } from "../api/types";
import { PluginConfigPanel, type PluginConfigPanelProps } from "./PluginConfigPanel";

const catalog: PluginConfigCatalog = {
  scope: "global",
  workspace_root: "/workspace",
  revision: "abcdef1234567890",
  applies_to: "new_sessions",
  plugins: [
    {
      plugin_id: "compact",
      name: "compact",
      editable: true,
      config_schema: {
        type: "object",
        properties: {
          automatic: { default: true, title: "Automatic", type: "boolean" },
          trigger_ratio: { default: 0.8, title: "Trigger Ratio", type: "number" },
        },
      },
      scope_config: { automatic: true },
      effective_config: { automatic: true },
      unavailable_reason: "",
    },
    {
      plugin_id: "sandbox",
      name: "sandbox",
      editable: true,
      config_schema: {
        type: "object",
        properties: { path: { title: "Path", type: "string" } },
        required: ["path"],
      },
      scope_config: {},
      effective_config: {},
      unavailable_reason: "",
    },
    {
      plugin_id: "skills",
      name: "skills",
      editable: false,
      config_schema: null,
      scope_config: {},
      effective_config: {},
      unavailable_reason: "This plugin does not declare a Pydantic Config model.",
    },
  ],
};

function renderPanel(props: Partial<PluginConfigPanelProps> = {}) {
  const load = vi.fn().mockResolvedValue(catalog);
  const update = vi.fn().mockImplementation(
    (_sessionId: string, _threadId: string, pluginId: string, _scope: string, _revision: string, config: Record<string, unknown>) =>
      Promise.resolve({
        ...catalog,
        revision: "second-revision",
        plugins: catalog.plugins.map((plugin) => (
          plugin.plugin_id === pluginId
            ? { ...plugin, scope_config: config, effective_config: config }
            : plugin
        )),
      }),
  );
  render(
    <PluginConfigPanel sessionId="s1" threadId="t1" scope="global" load={load} update={update} {...props} />,
  );
  return { load, update };
}

const pluginList = () => screen.getByRole("navigation", { name: "Plugin configurations" });
const listedPlugins = () => within(pluginList()).getAllByRole("button").map((button) => button.textContent);

describe("PluginConfigPanel", () => {
  it("offers the declared plugins and folds away the rest", async () => {
    renderPanel();

    await screen.findByRole("button", { name: "compact" });
    expect(listedPlugins()).toEqual(["compact", "sandbox"]);
    fireEvent.click(screen.getByText("1 plugin declares no configuration"));
    fireEvent.click(screen.getByRole("button", { name: "skills" }));
    expect(screen.getByText("This plugin does not declare a Pydantic Config model.")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Save plugin configuration" })).toBeNull();
  });

  it("leaves plugins another editor owns out of the list", async () => {
    renderPanel({ ownedPluginIds: ["sandbox"], ownedNote: "Sandbox is edited by the policy above." });

    await screen.findByRole("button", { name: "compact" });
    expect(listedPlugins()).toEqual(["compact"]);
    expect(screen.getByText("Sandbox is edited by the policy above.")).toBeVisible();
  });

  it("saves only a changed layer and can discard the change", async () => {
    const { update } = renderPanel();
    await screen.findByRole("button", { name: "compact" });

    const save = screen.getByRole("button", { name: "Save plugin configuration" });
    expect(save).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Discard" })).toBeNull();

    fireEvent.click(screen.getByLabelText("Automatic"));
    expect(save).toBeEnabled();
    expect(screen.getByText("Unsaved changes")).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: "Discard" }));
    expect(screen.getByRole("button", { name: "Save plugin configuration" })).toBeDisabled();

    fireEvent.click(screen.getByLabelText("Automatic"));
    fireEvent.click(screen.getByRole("button", { name: "Save plugin configuration" }));
    await waitFor(() => expect(update).toHaveBeenCalledWith(
      "s1", "t1", "compact", "global", "abcdef1234567890", { automatic: false },
    ));
    expect(await screen.findByText("Saved")).toBeVisible();
    expect(screen.getByRole("button", { name: "Save plugin configuration" })).toBeDisabled();
  });

  it("blocks saving while a required field is empty", async () => {
    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: "sandbox" }));

    fireEvent.change(screen.getByLabelText("Path"), { target: { value: "/tmp" } });
    expect(screen.getByRole("button", { name: "Save plugin configuration" })).toBeEnabled();

    fireEvent.change(screen.getByLabelText("Path"), { target: { value: "" } });
    expect(screen.getByText("Path is required.")).toBeVisible();
    expect(screen.getByText("1 field needs attention")).toBeVisible();
    expect(screen.getByRole("button", { name: "Save plugin configuration" })).toBeDisabled();
  });

  it("keeps the form usable when the advanced JSON is invalid", async () => {
    renderPanel();
    await screen.findByRole("button", { name: "compact" });

    fireEvent.click(screen.getByText("Advanced JSON"));
    const editor = screen.getByLabelText("Plugin configuration JSON");
    fireEvent.change(editor, { target: { value: "{ broken" } });
    expect(screen.getByRole("alert")).toBeVisible();
    expect(screen.getByText("Unsaved changes")).toBeVisible();
    expect(screen.getByRole("button", { name: "Save plugin configuration" })).toBeDisabled();
    expect(screen.getByLabelText("Automatic")).toBeChecked();

    fireEvent.change(editor, { target: { value: '{"automatic": false}' } });
    expect(screen.getByLabelText("Automatic")).not.toBeChecked();
    expect(screen.getByRole("button", { name: "Save plugin configuration" })).toBeEnabled();
  });

  it("reports a catalog failure instead of an empty editor", async () => {
    renderPanel({ load: vi.fn().mockRejectedValue(new Error("catalog unavailable")) });

    expect(await screen.findByText("catalog unavailable")).toBeVisible();
    expect(screen.queryByRole("navigation", { name: "Plugin configurations" })).toBeNull();
  });
});
