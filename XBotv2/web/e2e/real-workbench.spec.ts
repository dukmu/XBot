import { expect, test } from "@playwright/test";

test("real service: stream, tool, media and resume", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "New session" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Create" }).click();

  const composer = page.getByRole("textbox", { name: "Message XBot" });
  await expect(composer).toBeEnabled();
  await composer.fill("Verify the real Web transport");
  await composer.press("Enter");
  await expect(page.getByText("A real MockLLM response through the XBot HTTP stream.", { exact: true })).toBeVisible();
  const tool = page.locator('[data-tool="read"]').first();
  await expect(tool).toBeVisible();
  await tool.locator("summary").click();
  await expect(tool.getByText("browser tool result")).toBeVisible();

  await composer.evaluate((element) => {
    const transfer = new DataTransfer();
    const bytes = Uint8Array.from(atob("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="), (character) => character.charCodeAt(0));
    transfer.items.add(new File([bytes], "clipboard.png", { type: "image/png" }));
    element.dispatchEvent(new ClipboardEvent("paste", { bubbles: true, cancelable: true, clipboardData: transfer }));
  });
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByText("The clipboard image reached the real MockLLM.", { exact: true })).toBeVisible();
  const sessionId = (await (await page.request.get("/api/sessions")).json()).sessions[0].session_id;
  const history = await (await page.request.get(`/api/sessions/${sessionId}/threads/main/messages`)).json();
  expect(history.items.some((item: { kind: string; media?: unknown[] }) => item.kind === "human_input" && item.media?.length)).toBe(true);

  await page.reload();
  await expect(page.getByText("Verify the real Web transport", { exact: true })).toBeVisible();
  await expect(page.getByText("The clipboard image reached the real MockLLM.", { exact: true })).toBeVisible();
  await expect(page.locator('[data-tool="read"]').first()).toBeVisible();
  await expect(page.locator('img[src*="/artifacts/"]').first()).toBeVisible();
  await expect(composer).toBeEnabled();
  await composer.fill("Continue with a second real turn");
  await composer.press("Enter");
  await expect(page.getByText("The second real turn stayed aligned with the first.", { exact: true })).toBeVisible();
});
