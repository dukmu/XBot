import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { InteractionRequest } from "../api/types";
import {
  ALLOW_ONCE,
  ALLOW_SESSION,
  APPROVAL_DETAILS,
  ANSWER_PLACEHOLDER,
  PendingInteraction,
  REJECT,
  SUBMIT,
} from "./PendingInteraction";

const permission: InteractionRequest = {
  kind: "permission",
  request_id: "perm-1",
  source: "escalate",
  reason: "Need to write the notes.txt file as requested by the user.",
  tool_call: { id: "call-1", name: "shell", args: { command: "echo hi > notes.txt" } },
  resume_supported: true,
};

const question: InteractionRequest = {
  kind: "user_input",
  request_id: "user_input:call-2",
  source: "ask_user",
  tool_call_id: "call-2",
  question: "Which color do you prefer?",
  options: [
    { label: "Blue", description: "A cool recessive hue that reads as calm." },
    { label: "Green", description: "A restful mid-spectrum hue." },
  ],
  resume_supported: true,
};

/**
 * The ported inline interaction: an approval is a labelled `Approval details`
 * group, a question is a region named by the question with an option group and
 * a free-text answer. Both live in the transcript, not over it.
 */
describe("PendingInteraction", () => {
  it("renders the approval as a labelled group with its decisions", () => {
    const onResolve = vi.fn().mockResolvedValue(undefined);
    render(<PendingInteraction request={permission} onResolve={onResolve} />);
    const group = screen.getByRole("group", { name: APPROVAL_DETAILS });
    expect(group.textContent).toContain("Need to write the notes.txt file");
    expect(group.textContent).toContain("echo hi > notes.txt");
    fireEvent.click(screen.getByRole("button", { name: REJECT }));
    expect(onResolve).toHaveBeenCalledWith(permission, "deny", "once");
  });

  it("remembers an approval for the session when asked", async () => {
    const onResolve = vi.fn().mockResolvedValue(undefined);
    render(<PendingInteraction request={permission} onResolve={onResolve} />);
    fireEvent.click(screen.getByRole("button", { name: ALLOW_ONCE }));
    expect(onResolve).toHaveBeenLastCalledWith(permission, "allow", "once");
    // The card locks itself while the answer is in flight.
    const session = screen.getByRole("button", { name: ALLOW_SESSION });
    await waitFor(() => expect(session).toHaveProperty("disabled", false));
    fireEvent.click(session);
    expect(onResolve).toHaveBeenLastCalledWith(permission, "allow", "session");
  });

  it("names the question region and submits the chosen option", () => {
    const onResolve = vi.fn().mockResolvedValue(undefined);
    render(<PendingInteraction request={question} onResolve={onResolve} />);
    const region = screen.getByRole("region", { name: "Which color do you prefer?" });
    expect(region.textContent).toContain("A cool recessive hue that reads as calm.");
    const submit = screen.getByRole("button", { name: SUBMIT });
    expect(submit).toHaveProperty("disabled", true);
    fireEvent.click(screen.getByRole("radio", { name: /Blue/ }));
    expect(submit).toHaveProperty("disabled", false);
    fireEvent.click(submit);
    expect(onResolve).toHaveBeenCalledWith(question, "Blue", "once");
  });

  it("submits a typed answer instead of an option", () => {
    const onResolve = vi.fn().mockResolvedValue(undefined);
    render(<PendingInteraction request={question} onResolve={onResolve} />);
    const box = screen.getByRole("textbox", { name: ANSWER_PLACEHOLDER });
    fireEvent.change(box, { target: { value: "Purple, with notes" } });
    expect(screen.getByRole("radio", { name: /Blue/ })).toHaveProperty("checked", false);
    fireEvent.click(screen.getByRole("button", { name: SUBMIT }));
    expect(onResolve).toHaveBeenCalledWith(question, "Purple, with notes", "once");
  });
});
