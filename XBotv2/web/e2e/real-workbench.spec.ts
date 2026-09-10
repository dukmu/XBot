import { expect, test } from "@playwright/test";

test("creates, streams, and visually preserves a real multi-turn session", async ({ page }, testInfo) => {
  await page.goto("/");

  const external = await page.request.post("/api/sessions", { data: {
    session_id: "external-session",
    thread_id: "main",
    mode: "new",
  } });
  expect(external.ok()).toBe(true);
  await expect(page.getByTitle("external-session")).toBeVisible();
  const externalDelete = await page.request.delete("/api/sessions/external-session");
  expect(externalDelete.ok()).toBe(true);
  await expect(page.getByTitle("external-session")).toHaveCount(0);

  await page.getByRole("main").getByRole("button", { name: "New session" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Create" }).click();

  const composer = page.getByRole("textbox", { name: "Message XBot" });
  await expect(composer).toBeVisible();
  await composer.fill("Verify the real Web transport");
  await composer.press("Enter");
  await expect(page.getByText("A real MockLLM response through the XBot HTTP stream.", { exact: true })).toBeVisible();

  const sessionsResponse = await page.request.get("/api/sessions");
  expect(sessionsResponse.ok()).toBe(true);
  const sessions = (await sessionsResponse.json()).sessions;
  expect(sessions).toHaveLength(1);
  const sessionId = sessions[0].session_id as string;
  const workspacesResponse = await page.request.get("/api/workspaces");
  expect(workspacesResponse.ok()).toBe(true);
  const workspaces = (await workspacesResponse.json()).items;
  expect(workspaces).toHaveLength(1);
  expect(workspaces[0].session_ids).toEqual([sessionId]);
  await expect(page.locator(".workspace-toggle")).toContainText("workspace");

  await composer.evaluate((element) => {
    const transfer = new DataTransfer();
    const encoded = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=";
    const bytes = Uint8Array.from(atob(encoded), (character) => character.charCodeAt(0));
    transfer.items.add(new File([bytes], "clipboard.png", { type: "image/png" }));
    element.dispatchEvent(new ClipboardEvent("paste", { bubbles: true, cancelable: true, clipboardData: transfer }));
  });
  await expect(page.getByRole("img", { name: "clipboard.png" })).toBeVisible();
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByText("The clipboard image reached the real MockLLM.", { exact: true })).toBeVisible();

  const conversation = page.locator("[data-conversation-scroll]");
  await expect(conversation).toBeVisible();
  await composer.fill("Continue with a second real turn");
  const secondTurnStarted = Date.now();
  await composer.press("Enter");
  await expect(page.getByText("The second real turn stayed aligned with the first.", { exact: true })).toBeVisible();
  expect(Date.now() - secondTurnStarted).toBeGreaterThanOrEqual(150);
  const think = page.getByRole("button", { name: /Think/ }).last();
  await think.click();
  await expect(page.locator(".reasoning-content").filter({ hasText: "I am comparing both persisted turns." })).toBeVisible();

  const geometry = await conversation.evaluate((element) => {
    const nodes = [...element.querySelectorAll<HTMLElement>(".timeline-node")]
      .map((node) => node.getBoundingClientRect());
    return {
      overflow: element.scrollWidth - element.clientWidth,
      overlaps: nodes.some((node, index) => index > 0 && node.top < nodes[index - 1].bottom - 1),
      scrollHeight: element.scrollHeight,
      clientHeight: element.clientHeight,
    };
  });
  expect(geometry.overflow).toBeLessThanOrEqual(1);
  expect(geometry.overlaps).toBe(false);

  await conversation.evaluate((element) => {
    element.scrollTop = 0;
    element.dispatchEvent(new Event("scroll", { bubbles: true }));
  });
  if (geometry.scrollHeight > geometry.clientHeight) {
    await expect(page.getByRole("button", { name: "Jump to latest activity" })).toBeVisible();
  }
  await page.screenshot({ path: testInfo.outputPath("real-multiturn.png"), fullPage: true });
  await page.getByRole("button", { name: "Jump to latest activity" }).click();
  await page.waitForTimeout(400);
  const latestPosition = await conversation.evaluate((element) => ({
    distance: element.scrollHeight - element.scrollTop - element.clientHeight,
    lastNodeBottom: element.querySelector<HTMLElement>(".timeline-node:last-of-type")?.getBoundingClientRect().bottom || 0,
    viewportBottom: element.getBoundingClientRect().bottom,
  }));
  expect(latestPosition.distance).toBeLessThanOrEqual(2);
  await page.screenshot({ path: testInfo.outputPath("real-multiturn-latest.png"), fullPage: true });

  await composer.fill("/help status");
  await composer.press("Enter");
  const help = page.getByRole("dialog", { name: "Commands" });
  await help.getByRole("button", { name: /^\/status\b/ }).click();
  await expect(composer).toHaveValue("/status");
  await composer.press("Enter");
  await expect(page.getByRole("region", { name: "/status result" })).toContainText(sessionId);
  await expect(page.locator(".notice-row")).toHaveCount(0);

  await page.reload();
  await page.getByTitle(sessionId).click();
  await expect(page.getByText("Verify the real Web transport", { exact: true })).toBeVisible();
  await expect(page.getByText("A real MockLLM response through the XBot HTTP stream.", { exact: true })).toBeVisible();

  await page.getByRole("button", { name: `More actions for ${sessionId}` }).click();
  await page.getByRole("menuitem", { name: "Delete" }).click();
  await page.getByRole("dialog", { name: "Delete this session?" }).getByRole("button", { name: "Delete session" }).click();
  await expect(page.getByText("No session selected", { exact: true })).toBeVisible();
  const afterDelete = await page.request.get("/api/sessions");
  expect((await afterDelete.json()).sessions).toEqual([]);
});
