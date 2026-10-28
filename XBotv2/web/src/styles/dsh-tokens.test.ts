/// <reference types="vite/client" />
import { describe, expect, it } from "vitest";
import main from "../main.tsx?raw";

/**
 * The WebUI's look comes from the token layer vendored from DeepSeek Harness.
 * The sheets are copied verbatim, so this test guards the wiring: the files are
 * present and `main.tsx` loads them before XBot's own sheets.  (Their CSS text is
 * not readable here because the test runner replaces style imports; the built
 * bundle is checked for the token values instead.)
 */
const sheets = import.meta.glob("./dsh/*.css", { eager: true, query: "?raw", import: "default" });

describe("vendored dsh token layer", () => {
  it("keeps every vendored sheet in the tree", () => {
    expect(Object.keys(sheets).sort()).toEqual([
      "./dsh/base.css",
      "./dsh/design-platform.css",
      "./dsh/gradient-shadow-text.css",
      "./dsh/scrollbar.css",
      "./dsh/shiki.css",
    ]);
  });

  it("loads the token sheets before XBot's own sheets", () => {
    const index = (entry: string) => main.indexOf(entry);
    for (const sheet of [
      "styles/dsh/base.css",
      "styles/dsh/design-platform.css",
      "styles/dsh/scrollbar.css",
      "styles/dsh/shiki.css",
      "styles/dsh/gradient-shadow-text.css",
      "styles/global.css",
      "styles/dsh.css",
    ]) {
      expect(index(sheet), `${sheet} is not imported`).toBeGreaterThanOrEqual(0);
    }
    // Tokens resolve before the presentation sheets that consume them.
    const tokens = [
      index("styles/dsh/base.css"),
      index("styles/dsh/design-platform.css"),
      index("styles/dsh/scrollbar.css"),
      index("styles/dsh/shiki.css"),
      index("styles/dsh/gradient-shadow-text.css"),
    ];
    expect(Math.max(...tokens)).toBeLessThan(index("styles/global.css"));
    expect(Math.max(...tokens)).toBeLessThan(index("styles/dsh.css"));
  });
});
