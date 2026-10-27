import { expect, test } from "@playwright/test";

test("restores approval and applies undo and clear against real history", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "New session" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Create" }).click();
  const composer = page.getByRole("textbox", { name: "Message XBot" });
  await expect(composer).toBeEnabled();
  const session = await page.evaluate(() => JSON.parse(localStorage.getItem("xbotv2.openSession")!));
  const base = `/api/sessions/${session.session_id}/threads/${session.thread_id}`;
  const policy = await page.request.patch(`/api/sessions/${session.session_id}/policy`, { data: { permissions: { read: "ask" } } });
  expect(policy.ok()).toBeTruthy();
  expect((await policy.json()).effective_permissions.rules).toEqual(expect.arrayContaining([expect.objectContaining({ tool_pattern: "read", decision: "ask" })]));

  await composer.fill("First question with approval");
  await composer.press("Enter");
  let approval = page.getByRole("group", { name: "Approval details" });
  await expect(approval).toBeVisible();
  await expect(approval).toContainText("read");
  await page.reload();
  approval = page.getByRole("group", { name: "Approval details" });
  await expect(approval).toBeVisible();
  await approval.getByRole("button", { name: "Allow once" }).click();
  await expect(approval).toHaveCount(0);
  await expect(page.getByText("A real MockLLM response through the XBot HTTP stream.", { exact: true })).toBeVisible({ timeout: 12000 });
  const tool = page.locator('[data-tool="read"]').first();
  await tool.locator("summary").click();
  await expect(tool.getByText("browser tool result")).toBeVisible();

  await composer.fill("Second question to undo");
  await composer.press("Enter");
  await expect(page.getByText("The clipboard image reached the real MockLLM.", { exact: true })).toBeVisible();
  const messages = async () => (await (await page.request.get(`${base}/messages`)).json()).items;
  expect((await messages()).some((item: { content?: string }) => item.content === "Second question to undo")).toBe(true);
  const undo = page.getByRole("button", { name: "Undo last turn" });
  await expect(undo).toBeEnabled();
  await undo.click();
  await expect.poll(async () => (await messages()).some((item: { content?: string }) => item.content === "Second question to undo")).toBe(false);
  await expect(page.getByText("Second question to undo", { exact: true })).toHaveCount(0);
  await expect(page.getByText("First question with approval", { exact: true })).toBeVisible();

  await page.getByRole("button", { name: "Clear history", exact: true }).click();
  await page.getByRole("dialog", { name: "Clear this thread?" }).getByRole("button", { name: "Clear history" }).click();
  await expect.poll(async () => (await messages()).length).toBe(0);
  await expect(page.getByText("First question with approval", { exact: true })).toHaveCount(0);
  await expect(page.getByText("A real MockLLM response through the XBot HTTP stream.", { exact: true })).toHaveCount(0);
});
