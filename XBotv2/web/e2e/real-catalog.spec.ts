import { mkdtempSync, mkdirSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { expect, test } from "@playwright/test";

test("browses a later workspace page without leaving the active session", async ({ page }) => {
  test.setTimeout(60000);
  const root = mkdtempSync(join(tmpdir(), "xbot-catalog-browser-"));
  try {
    for (let index = 0; index < 101; index++) {
      const path = join(root, `workspace-${String(index).padStart(3, "0")}`);
      mkdirSync(path);
      const response = await page.request.post("/api/workspaces", { data: { path } });
      expect(response.ok()).toBe(true);
    }
    await page.goto("/");
    await page.getByRole("button", { name: "New session" }).click();
    await page.getByRole("dialog").getByRole("button", { name: "Create" }).click();
    const composer = page.getByRole("textbox", { name: "Message XBot" });
    await expect(composer).toBeEnabled();
    const activeSession = await page.evaluate(() => JSON.parse(localStorage.getItem("xbotv2.openSession")!).session_id as string);
    await page.getByRole("button", { name: "Next workspaces" }).click();
    await expect(page.getByText("workspace-100", { exact: true })).toBeVisible();
    await page.getByText("workspace-100", { exact: true }).click();
    await expect(page.getByText("No sessions on this page")).toBeVisible();
    expect(await page.evaluate(() => JSON.parse(localStorage.getItem("xbotv2.openSession")!).session_id)).toBe(activeSession);
    await expect(composer).toBeEnabled();
    await page.getByRole("button", { name: "Previous workspaces" }).click();
    await expect(page.getByText("workspace-000", { exact: true })).toBeVisible();
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});
