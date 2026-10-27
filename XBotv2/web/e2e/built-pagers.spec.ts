import { join } from "node:path";
import { expect, test } from "@playwright/test";

for (const width of [280, 264]) {
  test(`built pager controls stay inside a ${width}px sidebar`, async ({ page }, testInfo) => {
    const session = process.env.XBOT_THREAD_FIXTURE_SESSION;
    test.skip(!session, "Set XBOT_THREAD_FIXTURE_SESSION to a persisted session");
    if (!session) return;
    await page.addInitScript(({ sessionId, sidebarWidth }) => {
      localStorage.setItem("xbot.sidebar.width", String(sidebarWidth));
      localStorage.setItem("xbotv2.openSession", JSON.stringify({
        session_id: sessionId, thread_id: "main", workspace_root: "",
      }));
    }, { sessionId: session, sidebarWidth: width });
    await page.goto("/");
    const sidebar = page.locator(".session-sidebar");
    await expect(sidebar.getByLabel("Workspace pages")).toBeVisible();
    await expect(sidebar.getByLabel("Session pages")).toBeVisible();
    await expect(sidebar.getByLabel("Thread pages")).toBeVisible();
    await expect(sidebar.getByLabel("Thread pages").getByRole("button", { name: "Next threads" })).toBeEnabled();
    const sidebarBox = await sidebar.boundingBox();
    expect(sidebarBox).not.toBeNull();
    expect(sidebarBox!.width).toBe(width);
    for (const name of ["Workspace pages", "Session filters", "Session pages", "Thread pages"]) {
      const pager = sidebar.getByLabel(name);
      await pager.scrollIntoViewIfNeeded();
      const box = await pager.boundingBox();
      expect(box).not.toBeNull();
      for (const button of await pager.locator("button").all()) {
        await expect(button).toBeVisible();
        const child = await button.boundingBox();
        expect(child).not.toBeNull();
        expect(child!.x).toBeGreaterThanOrEqual(box!.x - 1);
        expect(child!.x + child!.width).toBeLessThanOrEqual(box!.x + box!.width + 1);
        expect(child!.x).toBeGreaterThanOrEqual(sidebarBox!.x - 1);
        expect(child!.x + child!.width).toBeLessThanOrEqual(sidebarBox!.x + sidebarBox!.width + 1);
        expect(child!.y).toBeGreaterThanOrEqual(sidebarBox!.y - 1);
        expect(child!.y + child!.height).toBeLessThanOrEqual(sidebarBox!.y + sidebarBox!.height + 1);
      }
    }
    const directory = process.env.XBOT_PAGER_SCREENSHOT_DIR;
    await page.screenshot({ path: directory ? join(directory, `pager-${width}.png`) : testInfo.outputPath(`pager-${width}.png`) });
  });
}
