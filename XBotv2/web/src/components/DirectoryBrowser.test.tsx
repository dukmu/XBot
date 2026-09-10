import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { DirectoryBrowser } from "./DirectoryBrowser";

describe("DirectoryBrowser", () => {
  it("navigates server directories and returns the selected folder", async () => {
    const listDirectory = vi.fn(async (path?: string) => ({
      path: path || "/workspace",
      parent: path === "/workspace/src" ? "/workspace" : "/",
      home: "/home/test",
      separator: "/" as const,
      entries: path === "/workspace/src" ? [] : [{
        name: "src",
        path: "/workspace/src",
        hidden: false,
      }],
      truncated: false,
    }));
    const onOpen = vi.fn();
    render(<DirectoryBrowser
      initialPath="/workspace"
      listDirectory={listDirectory}
      onOpen={onOpen}
      onClose={() => undefined}
    />);

    fireEvent.click(await screen.findByRole("button", { name: "Open src" }));
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Directory path" })).toHaveValue("/workspace/src"));
    fireEvent.click(screen.getByRole("button", { name: "Select" }));

    expect(onOpen).toHaveBeenCalledWith("/workspace/src");
  });
});
