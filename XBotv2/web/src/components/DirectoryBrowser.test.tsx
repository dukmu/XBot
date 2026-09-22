import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import {
  CANCEL,
  DIRECTORY_TITLE,
  DirectoryBrowser,
  EDIT_PATH,
  HOME_CRUMB,
  OPEN,
  SHOW_HIDDEN,
  directoryCrumbs,
} from "./DirectoryBrowser";

/** Two levels plus a home, as the host lists them. */
function listing(path: string) {
  return {
    path,
    parent: path === "/workspace/src" ? "/workspace" : "/",
    home: "/home/test",
    separator: "/" as const,
    entries: path === "/workspace"
      ? [
        { name: "src", path: "/workspace/src", hidden: false },
        { name: ".git", path: "/workspace/.git", hidden: true },
      ]
      : path === "/workspace/src"
        ? [{ name: "components", path: "/workspace/src/components", hidden: false }]
        : [],
    truncated: false,
  };
}

/**
 * The ported directory browser: heading, breadcrumb with the click-to-edit path
 * zone, the level's folders, the selected folder's children beside them, the
 * hidden-files toggle and the Cancel / Open footer.
 */
describe("DirectoryBrowser", () => {
  it("navigates server directories and returns the selected folder", async () => {
    const listDirectory = vi.fn(async (path?: string) => listing(path || "/workspace"));
    const onOpen = vi.fn();
    render(<DirectoryBrowser
      initialPath="/workspace"
      listDirectory={listDirectory}
      onOpen={onOpen}
      onClose={() => undefined}
    />);

    expect(screen.getByRole("heading", { name: DIRECTORY_TITLE })).toBeTruthy();
    // Outside the home subtree the crumb chain roots at the filesystem root.
    expect(await screen.findByRole("button", { name: "workspace" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "/" })).toBeTruthy();
    fireEvent.click(await screen.findByRole("button", { name: "src" }));
    // Selecting shows the folder's children beside the level.
    await waitFor(() => expect(screen.getAllByRole("list")).toHaveLength(2));
    fireEvent.click(screen.getByRole("button", { name: OPEN }));

    expect(onOpen).toHaveBeenCalledWith("/workspace/src");
  });

  it("descends from the child list and navigates by breadcrumb", async () => {
    const listDirectory = vi.fn(async (path?: string) => listing(path || "/workspace"));
    render(<DirectoryBrowser
      initialPath="/workspace"
      listDirectory={listDirectory}
      onOpen={vi.fn()}
      onClose={() => undefined}
    />);
    fireEvent.click(await screen.findByRole("button", { name: "src" }));
    fireEvent.click(await screen.findByRole("button", { name: "components" }));
    await waitFor(() => expect(listDirectory).toHaveBeenCalledWith("/workspace/src/components", expect.anything()));
  });

  it("edits the path through the ported Edit path affordance", async () => {
    const listDirectory = vi.fn(async (path?: string) => listing(path || "/workspace"));
    render(<DirectoryBrowser
      initialPath="/workspace"
      listDirectory={listDirectory}
      onOpen={vi.fn()}
      onClose={() => undefined}
    />);
    await screen.findByRole("button", { name: "src" });
    expect(screen.queryByRole("textbox", { name: "Directory path" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: EDIT_PATH }));
    const input = screen.getByRole("textbox", { name: "Directory path" });
    // The editor opens seeded with a trailing separator.
    expect(input).toHaveValue("/workspace/");
    fireEvent.change(input, { target: { value: "/workspace/src" } });
    fireEvent.submit(input.closest("form")!);
    await waitFor(() => expect(listDirectory).toHaveBeenCalledWith("/workspace/src", expect.anything()));
  });

  it("keeps hidden folders behind the toggle and closes from Cancel", async () => {
    const onClose = vi.fn();
    render(<DirectoryBrowser
      initialPath="/workspace"
      listDirectory={vi.fn(async (path?: string) => listing(path || "/workspace"))}
      onOpen={vi.fn()}
      onClose={onClose}
    />);
    expect(await screen.findByRole("button", { name: "src" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: ".git" })).toBeNull();
    const toggle = screen.getByRole("button", { name: SHOW_HIDDEN });
    expect(toggle.getAttribute("aria-pressed")).toBe("false");
    fireEvent.click(toggle);
    expect(await screen.findByRole("button", { name: ".git" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: CANCEL }));
    expect(onClose).toHaveBeenCalled();
  });
});

describe("directory crumbs", () => {
  it("starts at Home inside the home subtree", () => {
    expect(directoryCrumbs({ path: "/home/test/work/src", home: "/home/test", separator: "/" })).toEqual([
      { name: HOME_CRUMB, path: "/home/test" },
      { name: "work", path: "/home/test/work" },
      { name: "src", path: "/home/test/work/src" },
    ]);
  });

  it("shows the ancestry outside it, rooted at the filesystem root", () => {
    expect(directoryCrumbs({ path: "/workspace/src", home: "/home/test", separator: "/" })).toEqual([
      { name: "/", path: "/" },
      { name: "workspace", path: "/workspace" },
      { name: "src", path: "/workspace/src" },
    ]);
  });
});
