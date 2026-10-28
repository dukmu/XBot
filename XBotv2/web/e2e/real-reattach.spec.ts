import { expect, test } from "@playwright/test";

test("reloads an active streamed turn from its snapshot cursor", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "New session" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Create" }).click();
  const composer = page.getByRole("textbox", { name: "Message XBot" });
  await expect(composer).toBeEnabled();
  await composer.fill("Reload during streaming");
  await composer.press("Enter");
  await expect(page.getByText("A real MockLLM response", { exact: false })).toBeVisible();
  await expect(page.getByRole("button", { name: "Interrupt" })).toBeVisible();
  await page.reload();
  await expect(page.getByRole("button", { name: "Interrupt" })).toBeVisible();
  await expect(page.getByText("A real MockLLM response through the XBot HTTP stream.", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Send" })).toBeVisible();
});
