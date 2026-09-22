import { render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { DraftSession } from "./DraftSession";
import { EMPTY_USAGE } from "../api/types";

const props = {
  commands: [],
  usage: { ...EMPTY_USAGE },
  inputHistory: [],
};

describe("DraftSession", () => {
  it("boots into a session with its composer already on screen", () => {
    // Ported from the DeepSeek Harness hero: cold start is a session, so the
    // composer is present and locked rather than replaced by an empty page.
    const { getByTestId, container } = render(
      <DraftSession {...props} onChooseWorkspace={() => undefined} onOpenSessions={() => undefined} />,
    );
    expect(getByTestId("choose-workspace")).toBeTruthy();
    const textarea = container.querySelector("textarea");
    expect(textarea).not.toBeNull();
    expect((textarea as HTMLTextAreaElement).disabled).toBe(true);
    expect(container.textContent).toContain("Choose a workspace to start a session");
  });

  it("opens the workspace picker from the hero", () => {
    const onChooseWorkspace = vi.fn();
    const { getByTestId } = render(
      <DraftSession {...props} onChooseWorkspace={onChooseWorkspace} onOpenSessions={() => undefined} />,
    );
    getByTestId("choose-workspace").click();
    expect(onChooseWorkspace).toHaveBeenCalledTimes(1);
  });
});
