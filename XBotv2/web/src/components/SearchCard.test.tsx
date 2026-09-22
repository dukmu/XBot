import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import {
  CHAT_SEARCH_MAX_LINES,
  SearchCard,
  headTailCap,
  searchCard,
  searchCopyText,
  searchSummary,
} from "./SearchCard";

/** The ported grep scenario's shape: matches grouped per file. */
const files = [
  {
    path: "src/SearchBlock.tsx",
    matches: [
      { lineNumber: 16, line: "export const DEFAULT_SEARCH_MAX_LINES = 16" },
      { lineNumber: 138, line: "export function SearchBlock(props: SearchBlockProps) {" },
    ],
  },
  { path: "src/search-row.tsx", matches: [{ lineNumber: 34, line: "export function SearchRow({" }] },
];

describe("search card", () => {
  it("summarizes what the card holds and groups matches per file", () => {
    const { container } = render(<SearchCard kind="matches" truncated={false} files={files} />);
    expect(screen.getByText("3 matches · 2 files")).toBeTruthy();
    const headers = [...container.querySelectorAll(".search-file")];
    expect(headers.map((header) => header.textContent)).toEqual([
      "src/SearchBlock.tsx2",
      "src/search-row.tsx1",
    ]);
    // Each match reads `<line>: <text>`, as the ported card does.
    expect(container.querySelector(".search-line")?.textContent)
      .toBe("16: export const DEFAULT_SEARCH_MAX_LINES = 16");
  });

  it("collapses a file group and keeps its header", () => {
    const { container } = render(<SearchCard kind="matches" truncated={false} files={files} />);
    fireEvent.click(screen.getByRole("button", { name: /src\/SearchBlock.tsx/ }));
    expect(container.querySelectorAll(".search-line")).toHaveLength(1);
    expect(screen.getByRole("button", { name: /src\/SearchBlock.tsx/ }).getAttribute("aria-expanded"))
      .toBe("false");
  });

  it("caps long results head and tail with an expand control", () => {
    const many = [{
      path: "big.ts",
      matches: Array.from({ length: 20 }, (_, index) => ({ lineNumber: index + 1, line: `line ${index + 1}` })),
    }];
    const { container } = render(<SearchCard kind="matches" truncated={false} files={many} />);
    // 21 rows against the chat cap of 8: 4 head, 4 tail, 13 hidden.
    expect(container.querySelectorAll(".search-line, .search-file")).toHaveLength(8);
    const expand = screen.getByRole("button", { name: "Expand the remaining 13 rows" });
    expect(expand.textContent).toBe("… 13 more rows");
    fireEvent.click(expand);
    expect(container.querySelectorAll(".search-line, .search-file")).toHaveLength(21);
  });

  it("lists paths for a name search and says when the search was capped", () => {
    render(<SearchCard kind="paths" truncated paths={["a.ts", "b/c.ts"]} />);
    expect(screen.getByText("2 paths")).toBeTruthy();
    expect(screen.getByText("a.ts")).toBeTruthy();
    expect(screen.getByRole("status").textContent).toContain("more matches exist");
  });

  it("copies the plain-text form of the card", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    render(<SearchCard kind="matches" truncated={false} files={files} />);
    fireEvent.click(screen.getByRole("button", { name: "Copy" }));
    expect(writeText).toHaveBeenCalledWith(searchCopyText({ kind: "matches", truncated: false, files }));
  });

  it("derives the card from XBot's structured search result", () => {
    expect(searchCard({
      name: "search",
      data: {
        kind: "directory",
        returned_matches: 2,
        truncated: true,
        matches: [
          { path: "a.ts", line: 3, column: 1, text: "one" },
          { path: "a.ts", line: 9, column: 2, text: "two" },
        ],
      },
    })).toEqual({
      kind: "matches",
      truncated: true,
      files: [{ path: "a.ts", matches: [{ lineNumber: 3, line: "one" }, { lineNumber: 9, line: "two" }] }],
    });
    expect(searchCard({ name: "search", data: { files: ["a.ts"], returned_files: 1, truncated: false } }))
      .toEqual({ kind: "paths", truncated: false, paths: ["a.ts"] });
    // A non-search tool, or a search whose result carries neither shape, stays
    // on the generic row.
    expect(searchCard({ name: "read", data: { matches: [] } })).toBeNull();
    expect(searchCard({ name: "search", data: { ok: false, error: "boom" } })).toBeNull();
  });

  it("keeps the ported cap arithmetic and summary units", () => {
    expect(headTailCap(20, CHAT_SEARCH_MAX_LINES, false)).toEqual({ hidden: 12, capped: true, headLines: 4, tailLines: 4 });
    expect(headTailCap(4, CHAT_SEARCH_MAX_LINES, false).hidden).toBe(-4);
    expect(searchSummary({ kind: "matches", truncated: true, files })).toBe("first 3 matches · 2 files");
    expect(searchSummary({ kind: "paths", truncated: false, paths: ["a"] })).toBe("1 path");
  });
});
