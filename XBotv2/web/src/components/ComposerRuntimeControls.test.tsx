import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ComposerRuntimeControls } from "./ComposerRuntimeControls";
import type { AgentInfo, ProviderInfo } from "../api/types";
import type { RuntimeSession } from "../state/runtime";

const providers = [
  {
    name: "deepseek",
    provider: "deepseek",
    default_model: "deepseek-v4-flash",
    models: [
      { model: "deepseek-v4-flash", reasoning_effort: "high", effort: ["high", "low"], input_modalities: [] },
    ],
  },
] as unknown as ProviderInfo[];

const agents = [
  { name: "default", mode: "primary", description: "Full coding agent with file editing and shell." },
  { name: "minimal", mode: "primary", description: "Two-tool coding agent." },
  { name: "reviewer", mode: "subagent", description: "Reads diffs." },
] as unknown as AgentInfo[];

const current = {
  provider: "deepseek",
  model: "deepseek-v4-flash",
  model_mode: "",
  agent_name: "default",
} as unknown as RuntimeSession;

const handlers = {
  onAgent: async () => undefined,
  onProvider: async () => undefined,
  onEffort: async () => undefined,
};

describe("ComposerRuntimeControls", () => {
  it("announces the mode and model the way the ported UI does", () => {
    const { getByLabelText } = render(
      <ComposerRuntimeControls
        current={current}
        agents={agents}
        providers={providers}
        disabled={false}
        {...handlers}
      />,
    );
    expect(getByLabelText("Standard mode, current: default")).toBeTruthy();
    expect(getByLabelText("Select model, current deepseek / deepseek-v4-flash")).toBeTruthy();

    // The ported menu lists every primary definition with its description, and
    // subagent definitions are not selectable as the running mode.
    fireEvent.click(getByLabelText("Standard mode, current: default"));
    const items = screen.getAllByRole("menuitem");
    expect(items.map((item) => item.textContent)).toEqual([
      "defaultFull coding agent with file editing and shell.",
      "minimalTwo-tool coding agent.",
    ]);
    expect(items[0].getAttribute("aria-current")).toBe("true");
  });

  it("switches the running mode from the ported menu", async () => {
    const onAgent = vi.fn().mockResolvedValue(undefined);
    render(
      <ComposerRuntimeControls
        current={current}
        agents={agents}
        providers={providers}
        disabled={false}
        {...handlers}
        onAgent={onAgent}
      />,
    );
    fireEvent.click(screen.getByLabelText("Standard mode, current: default"));
    fireEvent.click(screen.getByRole("menuitem", { name: /minimal/ }));
    expect(onAgent).toHaveBeenCalledWith("minimal");
  });

  it("shows them locked before a session exists", () => {
    const { getByLabelText } = render(
      <ComposerRuntimeControls
        current={null}
        agents={agents}
        providers={providers}
        disabled
        {...handlers}
      />,
    );
    // With no session the trigger names the mode alone, as the ported hero does.
    const mode = getByLabelText("Standard mode") as HTMLButtonElement;
    expect(mode.disabled).toBe(true);
    expect(getByLabelText("Select model, current Select model")).toBeTruthy();
  });
});

/**
 * The ported `declared-reasoning` control: effort is a radio menu whose items
 * are the levels the model declares, with an explicit `Default` for the model's
 * own level.
 */
describe("reasoning effort menu", () => {
  const withEffort = [
    {
      name: "deepseek",
      provider: "deepseek",
      default_model: "deepseek-v4-flash",
      models: [
        {
          model: "deepseek-v4-flash",
          reasoning_effort: "high",
          effort: ["Off", "High", "Max"],
          input_modalities: [],
        },
      ],
    },
  ] as unknown as ProviderInfo[];

  it("lists the declared levels beside an explicit default", () => {
    render(
      <ComposerRuntimeControls
        current={current}
        agents={agents}
        providers={withEffort}
        disabled={false}
        {...handlers}
      />,
    );
    fireEvent.click(screen.getByLabelText("Reasoning effort, current: Default"));
    const items = screen.getAllByRole("menuitemradio");
    expect(items.map((item) => item.textContent)).toEqual(["Default", "Off", "High", "Max"]);
    expect(items[0].getAttribute("aria-checked")).toBe("true");
  });

  it("reports the chosen level, and the default as no level", () => {
    const onEffort = vi.fn().mockResolvedValue(undefined);
    const { unmount } = render(
      <ComposerRuntimeControls
        current={current}
        agents={agents}
        providers={withEffort}
        disabled={false}
        {...handlers}
        onEffort={onEffort}
      />,
    );
    fireEvent.click(screen.getByLabelText("Reasoning effort, current: Default"));
    fireEvent.click(screen.getByRole("menuitemradio", { name: /Max/ }));
    expect(onEffort).toHaveBeenCalledWith("Max");
    unmount();

    const running = render(
      <ComposerRuntimeControls
        current={{ ...current, model_mode: "Max" } as unknown as RuntimeSession}
        agents={agents}
        providers={withEffort}
        disabled={false}
        {...handlers}
        onEffort={onEffort}
      />,
    );
    expect(screen.getByLabelText("Reasoning effort, current: Max")).toBeTruthy();
    fireEvent.click(screen.getByLabelText("Reasoning effort, current: Max"));
    fireEvent.click(screen.getByRole("menuitemradio", { name: "Default" }));
    expect(onEffort).toHaveBeenLastCalledWith("");
    running.unmount();
  });
});
