import { expect, test } from "@playwright/test";

test("built Web opens a later-page subagent whose parent is on another page", async ({ page }, testInfo) => {
  const session = process.env.XBOT_THREAD_FIXTURE_SESSION;
  const parent = process.env.XBOT_THREAD_FIXTURE_PARENT;
  const child = process.env.XBOT_THREAD_FIXTURE_CHILD;
  test.skip(!session || !parent || !child, "Set XBOT_THREAD_FIXTURE_SESSION, XBOT_THREAD_FIXTURE_PARENT and XBOT_THREAD_FIXTURE_CHILD");
  if (!session || !parent || !child) return;
  const initial = await page.request.get(`/api/sessions/${encodeURIComponent(session)}/threads?limit=100`);
  expect(initial.ok()).toBe(true);
  const first = await initial.json();
  expect(first.threads).toHaveLength(100);
  expect(first.threads.some((thread: { thread_id: string }) => thread.thread_id === parent)).toBe(true);
  const laterResponse = await page.request.get(`/api/sessions/${encodeURIComponent(session)}/threads?limit=100&cursor=${encodeURIComponent(first.next_cursor)}`);
  const later = await laterResponse.json();
  expect(later.threads.some((thread: { thread_id: string }) => thread.thread_id === parent)).toBe(false);
  expect(later.threads.find((thread: { thread_id: string }) => thread.thread_id === child).parent_thread_id).toBe(parent);

  await page.addInitScript((sessionId) => localStorage.setItem("xbotv2.openSession", JSON.stringify({
    session_id: sessionId, thread_id: "main", workspace_root: "",
  })), session);
  await page.goto("/");
  const main = page.getByRole("main");
  const next = main.getByRole("button", { name: "Next threads" });
  await expect(next).toBeEnabled();
  await next.click();
  await expect(main.getByRole("button", { name: "Previous threads" })).toBeEnabled();
  await main.getByRole("button", { name: /subagents? on this page/ }).click();
  await expect(main.getByTestId(`subagent-${parent}`)).toHaveCount(0);
  await main.getByTestId(`subagent-${child}`).click();
  await expect(main.getByText("read-only · main thread keeps running")).toBeVisible();
  await expect(main.getByRole("navigation", { name: "Session hierarchy" })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("built-thread-page.png") });
});
