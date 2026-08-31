import { expect, test } from "@playwright/test";

test("creates, streams, commands, and restores through the real HTTP server", async ({ page }) => {
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
    transfer.items.add(new File([new Uint8Array([137, 80, 78, 71])], "clipboard.png", { type: "image/png" }));
    element.dispatchEvent(new ClipboardEvent("paste", { bubbles: true, cancelable: true, clipboardData: transfer }));
  });
  await expect(page.getByRole("img", { name: "clipboard.png" })).toBeVisible();
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByText("The clipboard image reached the real MockLLM.", { exact: true })).toBeVisible();

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
