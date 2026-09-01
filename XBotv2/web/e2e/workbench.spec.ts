import { expect, test, type Page, type Route } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  await mockProtocol(page);
  await page.goto("/");
});

test("collapses to the DSh rail and expands into focused session search", async ({ page }) => {
  test.skip((page.viewportSize()?.width || 0) <= 760, "mobile uses the overlay sidebar");
  await page.getByRole("button", { name: "Collapse sidebar" }).click();
  const railSearch = page.getByRole("button", { name: "Search sessions" });
  await expect(railSearch).toBeVisible();
  await railSearch.click();
  await expect(page.getByRole("textbox", { name: "Search sessions" })).toBeFocused();
});

test("renders an active workbench without overflow", async ({ page }, testInfo) => {
  await openDemoSession(page);
  if ((page.viewportSize()?.width || 0) <= 580) {
    await page.getByRole("button", { name: "Runtime settings" }).click();
    await expect(page.locator(".mobile-runtime-menu select")).toHaveCount(2);
    await page.getByRole("button", { name: "Runtime settings" }).click();
  }
  const composer = page.getByRole("textbox", { name: "Message XBot" });
  await composer.fill("Review the API boundary");
  await composer.press("Enter");

  await expect(page.getByText("Think", { exact: true })).toBeVisible();
  await page.getByText("Think", { exact: true }).click();
  await expect(page.locator(".reasoning-content").getByText("I am checking the public resources.", { exact: true })).toBeVisible();
  await expect(page.getByText(
    "The Web client remains behind the typed v3 API.",
    { exact: true },
  )).toHaveCount(1);
  await expect(page.getByText("filesystem_read", { exact: true })).toBeVisible();
  await expect(page.locator(".tool-details")).toHaveCount(0);
  await page.getByText("filesystem_read", { exact: true }).click();
  await expect(page.getByText("Path", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Background tasks" }).click();
  await expect(page.getByText("Explorer", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Background tasks" }).click();
  await page.getByRole("button", { name: /Context \d+% used/ }).click();
  await expect(page.getByRole("dialog", { name: "Context usage" })).toContainText("Cumulative input");
  await page.keyboard.press("Escape");
  await expect(page.locator(".status-bar")).toContainText("tokens:1.4k");

  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(0);
  await page.screenshot({ path: testInfo.outputPath("workbench.png"), fullPage: true });
});

test("answers a permission request through the interaction endpoint", async ({ page }) => {
  await openDemoSession(page);
  const composer = page.getByRole("textbox", { name: "Message XBot" });
  await composer.fill("Write a report");
  await composer.press("Enter");

  await expect(page.getByRole("heading", { name: "Approval required" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Approval required" })).toHaveCount(1);
  await expect(page.getByText("filesystem_write", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Allow once" }).click();
  await expect(page.getByRole("heading", { name: "Approval required" })).toBeHidden();
});

test("renders Todo state as a checklist", async ({ page }) => {
  await openDemoSession(page);
  const composer = page.getByRole("textbox", { name: "Message XBot" });
  await composer.fill("Plan the fix");
  await composer.press("Enter");

  await expect(page.getByText("update_todos", { exact: true })).toBeVisible();
  await expect(page.getByText("1/2 done · Verify the fix", { exact: true })).toBeVisible();
  await page.getByText("update_todos", { exact: true }).click();
  await expect(page.getByRole("region", { name: "Todo checklist" })).toContainText("Inspect the bug");
  await expect(page.getByRole("region", { name: "Todo checklist" })).toContainText("In progress");
  await expect(page.getByRole("region", { name: "Todo checklist" })).toContainText("Done");
});

test("restores historical sessions and their workspaces", async ({ page }) => {
  await openDemoSession(page);
  const mobile = (page.viewportSize()?.width || 0) <= 820;
  if (mobile) await page.getByRole("button", { name: "Open sessions" }).click();

  await page.getByTitle("history-session").click();
  await expect(page.getByText("Think", { exact: true })).toBeVisible();
  await page.getByText("Think", { exact: true }).click();
  await expect(page.locator(".reasoning-content").getByText("I checked the persisted context.", { exact: true })).toBeVisible();
  if (mobile) {
    await expect(page.getByText("A persisted answer from history.", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Open sessions" }).click();
    await expect(page.getByRole("button", { name: /Historical review.*workspace\/history/ })).toBeVisible();
  } else {
    await expect(page.getByRole("main").getByTitle("/workspace/history")).toBeVisible();
  }

  await page.getByTitle("demo-session").click();
  if (mobile) {
    await expect(page.getByText("Inspect API boundaries", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Open sessions" }).click();
    await expect(page.getByRole("button", { name: /Demo session.*workspace\/XBot/ })).toBeVisible();
  } else {
    await expect(page.getByRole("main").getByTitle("/workspace/XBot")).toBeVisible();
  }
  await expect(page.getByText("Inspect API boundaries", { exact: true })).toBeVisible();
});

test("renames and removes a workspace without deleting its sessions", async ({ page }) => {
  await openDemoSession(page);
  const mobile = (page.viewportSize()?.width || 0) <= 820;
  if (mobile) await page.getByRole("button", { name: "Open sessions" }).click();

  await page.getByRole("button", { name: "More actions for workspace XBot" }).click();
  await page.getByRole("menuitem", { name: "Move down" }).click();
  const workspaceRows = page.locator(".workspace-toggle");
  await expect(workspaceRows.nth(0)).toContainText("History");
  await expect(workspaceRows.nth(1)).toContainText("XBot");

  await page.getByRole("button", { name: "More actions for workspace XBot" }).click();
  await page.getByRole("menuitem", { name: "Rename" }).click();
  const title = page.getByRole("textbox", { name: "Workspace title" });
  await title.fill("XBot core");
  await page.getByRole("button", { name: "Save workspace title" }).click();
  await expect(page.getByRole("button", { name: "More actions for workspace XBot core" })).toBeVisible();

  await page.getByRole("button", { name: "More actions for workspace XBot core" }).click();
  await page.getByRole("menuitem", { name: "Remove" }).click();
  await expect(page.getByText("Sessions are kept.")).toBeVisible();
  await page.getByRole("button", { name: "Remove", exact: true }).click();
  await expect(page.getByTitle("demo-session")).toBeVisible();
});

test("renames a session through its row menu without refreshing the catalog", async ({ page }) => {
  await openDemoSession(page);
  const mobile = (page.viewportSize()?.width || 0) <= 820;
  if (mobile) await page.getByRole("button", { name: "Open sessions" }).click();

  await page.getByRole("button", { name: "More actions for Demo session" }).click();
  await page.getByRole("menuitem", { name: "Rename" }).click();
  const title = page.getByRole("textbox", { name: "Session title" });
  await title.fill("Renamed session");
  const renameRequest = page.waitForRequest((request) => (
    request.method() === "PATCH" && request.url().endsWith("/sessions/demo-session")
  ));
  await page.getByRole("button", { name: "Save session title" }).click();
  await renameRequest;
  await expect(page.getByRole("button", { name: /Renamed session.*workspace\/XBot/ })).toBeVisible();
});

test("persists manual session order within a workspace", async ({ page }) => {
  await openDemoSession(page);
  const mobile = (page.viewportSize()?.width || 0) <= 820;
  if (mobile) await page.getByRole("button", { name: "Open sessions" }).click();

  await page.getByRole("button", { name: "More actions for Demo session" }).click();
  const request = page.waitForRequest((candidate) => (
    candidate.method() === "POST"
    && candidate.url().endsWith("/workspaces/ws-xbot/sessions/demo-session/order")
  ));
  await page.getByRole("menuitem", { name: "Move down" }).click();
  await request;

  const rows = page.locator(".workspace-group").filter({ hasText: "XBot" }).locator(".session-row");
  await expect(rows.nth(0)).toContainText("Second session");
  await expect(rows.nth(1)).toContainText("Demo session");
});

test("reuses an unarchived blank session in the current workspace", async ({ page }) => {
  await openDemoSession(page);
  const openRequest = page.waitForRequest((request) => (
    request.method() === "POST"
    && request.url().endsWith("/sessions")
    && request.postDataJSON().session_id === "second-session"
  ));

  if ((page.viewportSize()?.width || 0) <= 820) {
    await page.getByRole("button", { name: "Open sessions" }).click();
  }
  await page.getByRole("button", { name: "New session" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Create" }).click();

  expect((await openRequest).postDataJSON()).toMatchObject({
    session_id: "second-session",
    workspace_root: "/workspace/XBot",
    mode: "resume",
  });
  await expect(page.locator(".message-block, .tool-block")).toHaveCount(0);
});

test("archives and restores a session without deleting its history", async ({ page }) => {
  await openDemoSession(page);
  const mobile = (page.viewportSize()?.width || 0) <= 820;
  if (mobile) await page.getByRole("button", { name: "Open sessions" }).click();

  await page.getByRole("button", { name: "More actions for Demo session" }).click();
  await page.getByRole("menuitem", { name: "Archive" }).click();
  await page.locator(".archived-sessions > summary").click();
  await expect(page.getByTitle("demo-session")).toBeVisible();
  await page.getByRole("button", { name: "More actions for Demo session" }).click();
  await page.getByRole("menuitem", { name: "Restore" }).click();
  await expect(page.getByTitle("demo-session")).toBeVisible();
  await expect(page.getByText("Inspect API boundaries", { exact: true })).toBeVisible();
});

test("discovers and executes server commands without sending a chat message", async ({ page }) => {
  await openDemoSession(page);
  const composer = page.getByRole("textbox", { name: "Message XBot" });
  const resourceReads: string[] = [];
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (request.method() === "GET" && !path.endsWith("/events")) resourceReads.push(path);
  });
  await composer.fill("/st");
  await expect(page.getByRole("option", { name: /status.*server/i })).toBeVisible();
  await composer.press("Tab");
  await expect(composer).toHaveValue("/status");
  const commandRequest = page.waitForRequest((request) => (
    request.method() === "POST" && request.url().endsWith("/commands")
  ));
  await composer.press("Enter");
  expect((await commandRequest).postDataJSON()).toMatchObject({ command: "status", raw: "/status", kind: "server" });
  await expect(page.getByRole("region", { name: "/status result" })).toContainText("session_id=demo-session thread_id=agent");
  await expect(page.locator(".notice-row")).toHaveCount(0);
  expect(resourceReads).toEqual([]);
});

test("shows help as a searchable command directory", async ({ page }, testInfo) => {
  await openDemoSession(page);
  const composer = page.getByRole("textbox", { name: "Message XBot" });
  await composer.fill("/help");
  await composer.press("Enter");
  const dialog = page.getByRole("dialog", { name: "Commands" });
  await expect(dialog).toBeVisible();
  await expect(dialog.locator("article")).toHaveCount(10);
  await page.screenshot({ path: testInfo.outputPath("command-help.png"), fullPage: true });
  await dialog.getByPlaceholder("Search name, description, or usage").fill("status");
  await expect(dialog.locator("article")).toHaveCount(1);
  await expect(dialog).toContainText("Show session status");
  await dialog.locator("article").getByRole("button").click();
  await expect(composer).toHaveValue("/status");
  await expect(composer).toBeFocused();
  await expect(dialog).toBeHidden();
  await expect(page.locator(".notice-row")).toHaveCount(0);
});

test("pastes a clipboard image directly into the composer", async ({ page }) => {
  await openDemoSession(page);
  const composer = page.getByRole("textbox", { name: "Message XBot" });
  await composer.evaluate((element) => {
    const transfer = new DataTransfer();
    transfer.items.add(new File([new Uint8Array([137, 80, 78, 71])], "clipboard.png", { type: "image/png" }));
    element.dispatchEvent(new ClipboardEvent("paste", {
      bubbles: true,
      cancelable: true,
      clipboardData: transfer,
    }));
  });
  const thumbnail = page.getByRole("img", { name: "clipboard.png" });
  await expect(thumbnail).toBeVisible();
  await thumbnail.click();
  await expect(page.getByRole("dialog", { name: "Preview clipboard.png" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog", { name: "Preview clipboard.png" })).toBeHidden();
  const messageRequest = page.waitForRequest((request) => request.method() === "POST" && request.url().endsWith("/messages"));
  await page.getByRole("button", { name: "Send" }).click();
  expect((await messageRequest).postDataJSON()).toMatchObject({
    images: [{ media_type: "image/png" }],
  });
});

test("accepts a file through the DSh whole-window drop surface", async ({ page }) => {
  await openDemoSession(page);
  await page.evaluate(() => {
    const transfer = new DataTransfer();
    transfer.items.add(new File(["report"], "drop.txt", { type: "text/plain" }));
    document.dispatchEvent(new DragEvent("dragenter", { bubbles: true, cancelable: true, dataTransfer: transfer }));
    (window as typeof window & { __xbotDrop?: DataTransfer }).__xbotDrop = transfer;
  });
  await expect(page.getByText("Drop files to attach", { exact: true })).toBeVisible();
  await page.evaluate(() => {
    const holder = window as typeof window & { __xbotDrop?: DataTransfer };
    document.dispatchEvent(new DragEvent("drop", { bubbles: true, cancelable: true, dataTransfer: holder.__xbotDrop }));
    delete holder.__xbotDrop;
  });
  await expect(page.locator('.composer-image[title="drop.txt"]')).toBeVisible();
  const messageRequest = page.waitForRequest((request) => request.method() === "POST" && request.url().endsWith("/messages"));
  await page.getByRole("button", { name: "Send" }).click();
  expect((await messageRequest).postDataJSON()).toMatchObject({
    attachments: [{ media_type: "text/plain", name: "drop.txt" }],
  });
});

test("regenerates the last turn through the authoritative history endpoint", async ({ page }) => {
  await openDemoSession(page);
  const regenerate = page.waitForRequest((request) => (
    request.method() === "POST" && request.url().endsWith("/history/regenerate")
  ));
  await page.getByRole("button", { name: "Regenerate response" }).click();
  await regenerate;
  await expect(page.getByText("Regenerated from the persisted input.", { exact: true })).toBeVisible();
  await expect(page.getByText("I will inspect the public SDK surface.", { exact: true })).toHaveCount(0);
  await expect(page.getByText("Inspect API boundaries", { exact: true })).toHaveCount(1);
});

test("does not rebuild history when a read-only command follows a turn", async ({ page }) => {
  await openDemoSession(page);
  const composer = page.getByRole("textbox", { name: "Message XBot" });
  await composer.fill("Inspect the current state");
  await composer.press("Enter");
  await expect(page.getByText("The Web client remains behind the typed v3 API.", { exact: true })).toBeVisible();
  const resourceReads: string[] = [];
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (request.method() === "GET" && !path.endsWith("/events")) resourceReads.push(path);
  });
  await composer.fill("/status");
  await composer.press("Enter");
  await expect(page.getByRole("region", { name: "/status result" })).toBeVisible();
  expect(resourceReads).toEqual([]);
});

test("contains long command output in a collapsible result panel", async ({ page }, testInfo) => {
  await openDemoSession(page);
  const composer = page.getByRole("textbox", { name: "Message XBot" });
  await composer.fill("/diagnostics");
  await composer.press("Enter");
  const result = page.getByRole("region", { name: "/diagnostics result" });
  const output = result.locator("pre");
  await expect(result).toContainText("120 lines");
  await expect(output).toHaveCount(0);
  await result.getByRole("button", { name: /diagnostics/i }).click();
  await expect(output).toBeVisible();
  expect(await output.evaluate((element) => element.scrollHeight > element.clientHeight)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("command-output.png"), fullPage: true });
  await result.getByRole("button", { name: /diagnostics/i }).click();
  await expect(output).toHaveCount(0);
  await expect(page.locator(".notice-row")).toHaveCount(0);
});

test("keeps long tool output head-tail bounded until explicitly expanded", async ({ page }) => {
  await openDemoSession(page);
  const composer = page.getByRole("textbox", { name: "Message XBot" });
  await composer.fill("Long tool output");
  await composer.press("Enter");

  await page.getByText("shell", { exact: true }).click();
  const details = page.locator(".tool-details");
  await expect(details).not.toContainText("line-20");
  await details.getByRole("button", { name: "Show 24 hidden lines" }).click();
  await expect(details).toContainText("line-20");
});

test("submits discovered prompt commands through the message stream", async ({ page }) => {
  await openDemoSession(page);
  const composer = page.getByRole("textbox", { name: "Message XBot" });
  const messageRequest = page.waitForRequest((request) => (
    request.method() === "POST" && request.url().endsWith("/messages")
  ));
  await composer.fill("/review API boundaries");
  await composer.press("Enter");
  expect((await messageRequest).postDataJSON()).toMatchObject({ content: "/review API boundaries" });
  await expect(page.getByText("The Web client remains behind the typed v3 API.", { exact: true })).toBeVisible();
});

test("uses client commands to resume a session with an explicit workspace", async ({ page }) => {
  await openDemoSession(page);
  const composer = page.getByRole("textbox", { name: "Message XBot" });
  const openRequest = page.waitForRequest((request) => {
    if (request.method() !== "POST" || !request.url().endsWith("/sessions")) return false;
    return request.postDataJSON().session_id === "history-session";
  });
  await composer.fill('/session history-session "/workspace/override"');
  await composer.press("Enter");
  expect((await openRequest).postDataJSON()).toMatchObject({
    session_id: "history-session",
    thread_id: "history-main",
    workspace_root: "/workspace/override",
    mode: "resume",
  });
  if ((page.viewportSize()?.width || 0) <= 580) {
    await page.getByRole("button", { name: "Runtime settings" }).click();
    await expect(page.locator(".mobile-runtime-workspace")).toContainText("/workspace/override");
  } else {
    await expect(page.getByTitle("/workspace/override")).toBeVisible();
  }
  await expect(page.getByText("A persisted answer from history.", { exact: true })).toBeVisible();
});

test("forks through the native session API and opens the new session", async ({ page }) => {
  await openDemoSession(page);
  const composer = page.getByRole("textbox", { name: "Message XBot" });
  const forkRequest = page.waitForRequest((request) => (
    request.method() === "POST" && request.url().endsWith("/sessions/demo-session/fork")
  ));
  const openRequest = page.waitForRequest((request) => (
    request.method() === "POST"
    && request.url().endsWith("/sessions")
    && request.postDataJSON().session_id === "fork-session"
  ));
  await composer.fill("/fork");
  await composer.press("Enter");
  await forkRequest;
  expect((await openRequest).postDataJSON()).toMatchObject({ session_id: "fork-session", mode: "resume" });
  await expect(page.getByText("Inspect API boundaries", { exact: true })).toBeVisible();
});

test("forks a session from its sidebar action menu", async ({ page }) => {
  await openDemoSession(page);
  if ((page.viewportSize()?.width || 0) <= 820) {
    await page.getByRole("button", { name: "Open sessions" }).click();
  }
  const forkRequest = page.waitForRequest((request) => (
    request.method() === "POST" && request.url().endsWith("/sessions/history-session/fork")
  ));
  await page.getByRole("button", { name: "More actions for Historical review" }).click();
  await page.getByRole("menuitem", { name: "Fork" }).click();
  await forkRequest;
  await expect(page.getByText("Inspect API boundaries", { exact: true })).toBeVisible();
});

test("deletes a persisted session from its sidebar action menu", async ({ page }) => {
  await openDemoSession(page);
  if ((page.viewportSize()?.width || 0) <= 820) {
    await page.getByRole("button", { name: "Open sessions" }).click();
  }
  await page.getByRole("button", { name: "More actions for Historical review" }).click();
  await page.getByRole("menuitem", { name: "Delete" }).click();
  const dialog = page.getByRole("dialog", { name: "Delete this session?" });
  await expect(dialog).toContainText("history, artifacts, and plugin state will be permanently deleted");
  const deleteRequest = page.waitForRequest((request) => (
    request.method() === "DELETE" && request.url().endsWith("/sessions/history-session")
  ));
  await dialog.getByRole("button", { name: "Delete session" }).click();
  await deleteRequest;
  await expect(page.getByTitle("history-session")).toHaveCount(0);
});

test("detaches the workbench after deleting the current session", async ({ page }) => {
  await openDemoSession(page);
  if ((page.viewportSize()?.width || 0) <= 820) {
    await page.getByRole("button", { name: "Open sessions" }).click();
  }
  await page.getByRole("button", { name: "More actions for Demo session" }).click();
  await page.getByRole("menuitem", { name: "Delete" }).click();
  await page.getByRole("dialog", { name: "Delete this session?" }).getByRole("button", { name: "Delete session" }).click();
  await expect(page.getByText("No session selected", { exact: true })).toBeVisible();
  await expect(page.getByRole("textbox", { name: "Message XBot" })).toHaveCount(0);
});

test("confirms clear history in the application and replaces the transcript", async ({ page }) => {
  await openDemoSession(page);
  const composer = page.getByRole("textbox", { name: "Message XBot" });
  await composer.fill("/clear");
  await composer.press("Enter");
  const dialog = page.getByRole("dialog", { name: "Clear this thread?" });
  await expect(dialog).toContainText("artifacts, and plugin state are preserved");
  await dialog.getByRole("button", { name: "Clear history" }).click();
  await expect(page.locator(".message-block, .tool-block")).toHaveCount(0);
  await expect(dialog).toBeHidden();
});

test("does not navigate away while a message request is active", async ({ page }) => {
  await openDemoSession(page);
  const composer = page.getByRole("textbox", { name: "Message XBot" });
  await composer.fill("Hold this turn open");
  await composer.press("Enter");
  if ((page.viewportSize()?.width || 0) <= 820) {
    await page.getByRole("button", { name: "Open sessions" }).click();
  }
  await page.getByTitle("history-session").click();
  await expect(page.locator(".ui-notification")).toHaveText(/Finish or interrupt the active turn before switching sessions/);
  await expect(page.locator(".notice-row")).toHaveCount(0);
  await expect(page.getByText("Inspect API boundaries", { exact: true })).toBeVisible();
});

test("does not navigate away while a server command is active", async ({ page }) => {
  await openDemoSession(page);
  let releaseCommand!: () => void;
  const commandGate = new Promise<void>((resolve) => {
    releaseCommand = resolve;
  });
  await page.route("**/commands", async (route) => {
    const request = route.request();
    if (request.method() === "POST" && request.postDataJSON().command === "diagnostics") {
      await commandGate;
    }
    await route.fallback();
  });
  const composer = page.getByRole("textbox", { name: "Message XBot" });
  await composer.fill("/diagnostics");
  await composer.press("Enter");
  await expect(page.getByText("Running /diagnostics", { exact: true })).toBeVisible();
  if ((page.viewportSize()?.width || 0) <= 820) {
    await page.getByRole("button", { name: "Open sessions" }).click();
  }
  await page.getByTitle("history-session").click();
  await expect(page.locator(".ui-notification")).toHaveText(/Wait for the active command before switching sessions/);
  await expect(page.getByText("Inspect API boundaries", { exact: true })).toBeVisible();
  releaseCommand();
  await expect(page.getByRole("region", { name: "/diagnostics result" })).toBeVisible();
});

test("manages the authoritative DSh queue while a turn is running", async ({ page }, testInfo) => {
  await openDemoSession(page);
  const composer = page.getByRole("textbox", { name: "Message XBot" });
  await composer.fill("Hold queue turn");
  await composer.press("Enter");
  await expect(page.getByRole("button", { name: "Interrupt" })).toBeVisible();

  await composer.fill("Queued follow-up");
  await composer.press("Enter");
  const dock = page.getByRole("region", { name: "Queued messages" });
  await expect(dock).toContainText("Queued follow-up");
  await dock.getByRole("button", { name: "Edit queued message" }).click();
  const editor = dock.getByRole("textbox", { name: "Edit queued message" });
  await editor.fill("Edited follow-up");
  await dock.getByRole("button", { name: "Save queued message" }).click();
  await expect(dock).toContainText("Edited follow-up");
  await page.screenshot({ path: testInfo.outputPath("queue-dock.png"), fullPage: true });
  await dock.getByRole("button", { name: "Remove queued message" }).click();
  await expect(dock).toBeHidden();
  await expect(page.getByText("The delayed turn completed.", { exact: true })).toBeVisible();
});

test("keeps a long history bounded while allowing older messages", async ({ page }) => {
  await openLongSession(page);
  await expect(page.locator(".message-block")).toHaveCount(160);
  await expect(page.getByRole("button", { name: "Older messages" })).toBeVisible();
  await page.getByRole("button", { name: "Older messages" }).click();
  await expect(page.locator(".message-block")).toHaveCount(160);
  await expect(page.getByText("Historical message 1", { exact: true })).toBeVisible();
});

async function openDemoSession(page: Page) {
  if ((page.viewportSize()?.width || 0) <= 820) {
    await page.getByRole("button", { name: "Open sessions" }).click();
  }
  await page.getByTitle("demo-session").click();
  await expect(page.getByRole("textbox", { name: "Message XBot" })).toBeVisible();
}

async function openLongSession(page: Page) {
  if ((page.viewportSize()?.width || 0) <= 820) {
    await page.getByRole("button", { name: "Open sessions" }).click();
  }
  await page.getByTitle("long-session").click();
  await expect(page.getByRole("textbox", { name: "Message XBot" })).toBeVisible();
}

async function mockProtocol(page: Page) {
  let demoMessageCount = 3;
  const deletedSessions = new Set<string>();
  const removedWorkspaces = new Set<string>();
  let xbotWorkspaceTitle = "XBot";
  let workspaceOrder = ["ws-xbot", "ws-history"];
  let xbotSessionOrder = ["demo-session", "second-session"];
  let demoSessionTitle = "Demo session";
  const archivedSessions = new Set<string>();
  let pendingInputs: Array<{
    message_id: string;
    content: string;
    target: "next-turn" | "next-step";
    source: string;
    image_count: number;
    artifact_count: number;
  }> = [];
  const runtimeEvents: Array<{
    type: string;
    data: Record<string, unknown>;
    requestId: string;
    sequence: number;
  }> = [];
  const publishRuntime = (
    events: Array<{ type: string; data: Record<string, unknown> }>,
    requestId: string,
  ) => {
    for (const event of events) {
      runtimeEvents.push({
        ...event,
        requestId,
        sequence: runtimeEvents.length + 1,
      });
    }
  };
  await page.route((url) => url.pathname.startsWith("/api/"), async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname.replace(/^\/api/, "");
    const method = request.method();

    if (path === "/hello") return json(route, { server_name: "xbotv2", protocol_version: "xbotv2.v3" });
    if (path === "/providers") return json(route, {
      default: "minimax",
      providers: [{
        name: "minimax",
        provider: "anthropic",
        default_model: "Minimax-M3",
        models: [{
          model: "Minimax-M3",
          max_context_tokens: 32768,
          max_output_tokens: 8192,
          reasoning_effort: "high",
          effort: ["low", "high"],
          thinking: "high",
          input_modalities: ["text", "image"],
        }],
      }],
    });
    if (path === "/workspaces" && method === "GET") return json(route, {
      items: [
        { workspace_id: "ws-xbot", path: "/workspace/XBot", title: xbotWorkspaceTitle, session_ids: xbotSessionOrder.filter((id) => !deletedSessions.has(id)), created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z" },
        { workspace_id: "ws-history", path: "/workspace/history", title: "History", session_ids: deletedSessions.has("history-session") ? [] : ["history-session"], created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z" },
      ].filter((workspace) => !removedWorkspaces.has(workspace.workspace_id))
        .sort((left, right) => workspaceOrder.indexOf(left.workspace_id) - workspaceOrder.indexOf(right.workspace_id)),
      archived_session_ids: [...archivedSessions],
      event_cursor: 0,
    });
    if (path === "/workspaces/ws-xbot" && method === "PATCH") {
      xbotWorkspaceTitle = String(request.postDataJSON().title);
      return json(route, { workspace: { workspace_id: "ws-xbot", path: "/workspace/XBot", title: xbotWorkspaceTitle, session_ids: ["demo-session"], created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:01Z" } });
    }
    if (path === "/workspaces/ws-xbot" && method === "DELETE") {
      removedWorkspaces.add("ws-xbot");
      return json(route, { workspace_id: "ws-xbot", status: "deleted" });
    }
    if (path === "/workspaces/ws-xbot/order" && method === "POST") {
      const before = request.postDataJSON().before_workspace_id as string | null;
      workspaceOrder = workspaceOrder.filter((workspaceId) => workspaceId !== "ws-xbot");
      const index = before === null ? workspaceOrder.length : workspaceOrder.indexOf(before);
      workspaceOrder.splice(index, 0, "ws-xbot");
      return json(route, { workspace_ids: workspaceOrder });
    }
    if (path === "/workspaces/ws-xbot/sessions/demo-session/order" && method === "POST") {
      const before = request.postDataJSON().before_session_id as string | null;
      xbotSessionOrder = xbotSessionOrder.filter((sessionId) => sessionId !== "demo-session");
      const index = before === null ? xbotSessionOrder.length : xbotSessionOrder.indexOf(before);
      xbotSessionOrder.splice(index, 0, "demo-session");
      return json(route, { workspace: { workspace_id: "ws-xbot", path: "/workspace/XBot", title: xbotWorkspaceTitle, session_ids: xbotSessionOrder, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:02Z" } });
    }
    if (path === "/sessions" && method === "GET") return json(route, {
      sessions: [
        { session_id: "demo-session", title: demoSessionTitle, workspace_root: "/workspace/XBot", status: "inactive", active_threads: 0, thread_count: 1, blank: false },
        { session_id: "second-session", title: "Second session", workspace_root: "/workspace/XBot", status: "inactive", active_threads: 0, thread_count: 1, blank: true },
        { session_id: "history-session", title: "Historical review", workspace_root: "/workspace/history", status: "inactive", active_threads: 0, thread_count: 1, blank: false },
        { session_id: "long-session", title: "Long history", workspace_root: "/workspace/long", status: "inactive", active_threads: 0, thread_count: 1, blank: false },
      ].filter((session) => !deletedSessions.has(session.session_id)),
      event_cursor: 0,
    });
    if (path === "/sessions/demo-session" && method === "PATCH") {
      demoSessionTitle = String(request.postDataJSON().title);
      return json(route, { session_id: "demo-session", title: demoSessionTitle, workspace_root: "/workspace/XBot", status: "active", active_threads: 1, thread_count: 1, blank: false });
    }
    if (path === "/sessions/demo-session/archive" && method === "PUT") {
      archivedSessions.add("demo-session");
      return json(route, { archived_session_ids: [...archivedSessions] });
    }
    if (path === "/sessions/demo-session/archive" && method === "DELETE") {
      archivedSessions.delete("demo-session");
      return json(route, { archived_session_ids: [...archivedSessions] });
    }
    if (path.startsWith("/sessions/") && method === "DELETE") {
      const sessionId = path.split("/")[2];
      deletedSessions.add(sessionId);
      return json(route, { session_id: sessionId, status: "deleted" });
    }
    if (path === "/sessions" && method === "POST") {
      const payload = request.postDataJSON();
      const sessionId = String(payload.session_id || "demo-session");
      return json(route, openSession(
        sessionId,
        String(payload.workspace_root || ""),
        Number(payload.history_limit || 0),
      ));
    }
    if (path === "/sessions/demo-session/threads") return json(route, {
      session_id: "demo-session",
      threads: [{
        session_id: "demo-session",
        thread_id: "agent",
        status: "active",
        kind: "main",
        turn_status: "idle",
        parent_thread_id: "",
        agent: "default",
        provider: "minimax",
        model: "Minimax-M3",
        model_mode: "high",
        context_window: 32000,
        message_count: demoMessageCount,
        usage: usage(),
        pending_interactions: [],
        status_slots: { goal: "active" },
      }],
    });
    if (path === "/sessions/history-session/threads") return json(route, {
      session_id: "history-session",
      threads: [{
        session_id: "history-session",
        thread_id: "history-main",
        status: "inactive",
        kind: "main",
        turn_status: "idle",
        parent_thread_id: "",
        agent: "default",
        provider: "minimax",
        model: "Minimax-M3",
        model_mode: "high",
        context_window: 32000,
        message_count: 2,
        usage: usage(),
        pending_interactions: [],
        status_slots: { goal: "active" },
        workspace_root: "/workspace/history",
        title: "Historical review",
      }],
    });
    if (path === "/sessions/second-session/threads") return json(route, {
      session_id: "second-session",
      threads: [{
        session_id: "second-session",
        thread_id: "agent",
        status: "inactive",
        kind: "main",
        turn_status: "idle",
        parent_thread_id: "",
        agent: "default",
        provider: "minimax",
        model: "Minimax-M3",
        model_mode: "high",
        context_window: 32000,
        message_count: 0,
        usage: usage(),
        pending_interactions: [],
        status_slots: {},
        workspace_root: "/workspace/XBot",
        title: "Second session",
      }],
    });
    if (path === "/sessions/long-session/threads") return json(route, {
      session_id: "long-session",
      threads: [{
        session_id: "long-session",
        thread_id: "long-main",
        status: "inactive",
        kind: "main",
        turn_status: "idle",
        parent_thread_id: "",
        agent: "default",
        provider: "minimax",
        model: "Minimax-M3",
        model_mode: "high",
        context_window: 32000,
        message_count: 200,
        usage: usage(),
        pending_interactions: [],
        status_slots: { goal: "active" },
        workspace_root: "/workspace/long",
        title: "Long history",
      }],
    });
    if (path === "/sessions/fork-session/threads") return json(route, {
      session_id: "fork-session",
      threads: [{
        session_id: "fork-session",
        thread_id: "agent",
        status: "inactive",
        kind: "main",
        turn_status: "idle",
        parent_thread_id: "",
        agent: "default",
        provider: "minimax",
        model: "Minimax-M3",
        model_mode: "high",
        context_window: 32000,
        message_count: 3,
        usage: usage(),
        pending_interactions: [],
        status_slots: { goal: "active" },
        workspace_root: "/workspace/XBot",
      }],
    });
    if (path === "/sessions/demo-session/fork" && method === "POST") return json(route, {
      session_id: "fork-session",
      source_session_id: "demo-session",
    });
    if (path === "/sessions/history-session/fork" && method === "POST") return json(route, {
      session_id: "fork-session",
      source_session_id: "history-session",
    });
    if (path.endsWith("/agents")) return json(route, {
      active: "default",
      agents: [{ name: "default", description: "Primary Agent", mode: "primary", provider: "", model: "", context_window: 32000 }],
    });
    if (path.endsWith("/commands") && method === "GET") return json(route, {
      commands: [
        { name: "status", slash: "/status", kind: "server", description: "Show session status", usage: "/status", examples: [], parameters: {} },
        { name: "diagnostics", slash: "/diagnostics", kind: "server", description: "Show runtime diagnostics", usage: "/diagnostics", examples: [], parameters: {} },
        { name: "review", slash: "/review", kind: "prompt", description: "Review the workspace", usage: "/review [focus]", examples: [], parameters: {} },
      ],
    });
    if (path.endsWith("/commands") && method === "POST") {
      const command = String(request.postDataJSON().command || "");
      return json(route, {
        type: "command_result",
        data: {
          command,
          status: "ok",
          effects: [],
          message: command === "diagnostics"
            ? Array.from({ length: 120 }, (_, index) => `diagnostic ${index + 1}`).join("\n")
            : "session_id=demo-session thread_id=agent",
        },
      });
    }
    if (path.endsWith("/messages") && method === "GET") {
      const sessionId = path.split("/")[2];
      const all = openSession(sessionId).history;
      const end = Number(url.searchParams.get("cursor") || all.length);
      const limit = Number(url.searchParams.get("limit") || all.length);
      const start = Math.max(0, end - limit);
      return json(route, {
        messages: all.slice(start, end),
        next_cursor: start ? String(start) : null,
      });
    }
    if (path.endsWith("/history/clear") && method === "POST") {
      demoMessageCount = 0;
      return json(route, { messages: [] });
    }
    if (path.endsWith("/history/regenerate") && method === "POST") {
      const events = [
        { type: "history_updated", data: { operation: "regenerate", turns: 1, history: [] } },
        { type: "message", data: { id: "regen-user", role: "user", content: "Inspect API boundaries", images: [], artifacts: [] } },
        { type: "turn_started", data: { turn: 1 } },
        { type: "assistant_message", data: { content: "Regenerated from the persisted input.", tool_calls: [] } },
        { type: "turn_finished", data: { turn: 1 } },
      ];
      const requestId = String(request.postDataJSON().request_id || "request-1");
      publishRuntime(events, requestId);
      return sse(route, events);
    }
    if (path.endsWith("/todos") && method === "GET") return json(route, {
      schema_version: 1,
      items: [],
    });
    if (path.endsWith("/tasks") && method === "GET") return json(route, {
      session_id: "demo-session",
      thread_id: "agent",
      tasks: [{
        task_id: "agent-1",
        kind: "agent",
        command: "Explorer: inspect protocol boundaries",
        cwd: "/workspace",
        status: "running",
        created_at: 1,
        started_at: 1,
        finished_at: 0,
        output: "Inspecting protocol models and client boundaries...",
        error: "",
        agent: "Explorer",
        thread_id: "subagent-explorer",
        usage: { total_tokens: 240 },
      }],
    });
    if (path.endsWith("/events")) {
      const after = Number(url.searchParams.get("after") || 0);
      return runtimeSse(
        route,
        runtimeEvents.filter((event) => event.sequence > after),
        after,
      );
    }
    const queueMatch = path.match(/^\/sessions\/[^/]+\/threads\/[^/]+\/queue\/([^/]+)$/);
    if (queueMatch && method === "PATCH") {
      const messageId = decodeURIComponent(queueMatch[1]);
      const payload = request.postDataJSON() as { action: "edit" | "remove" | "steer"; content?: string };
      if (payload.action === "remove") pendingInputs = pendingInputs.filter((item) => item.message_id !== messageId);
      else pendingInputs = pendingInputs.map((item) => item.message_id !== messageId ? item : {
        ...item,
        content: payload.action === "edit" ? String(payload.content || "") : item.content,
        target: payload.action === "steer" ? "next-step" : item.target,
      });
      publishRuntime([{ type: "queue_updated", data: { items: pendingInputs } }], messageId);
      return json(route, { items: pendingInputs });
    }
    if (path.endsWith("/messages") && method === "POST") {
      const payload = request.postDataJSON();
      const content = String(payload.content || "");
      const requestId = String(payload.request_id || "request-1");
      if (payload.delivery === "queue") {
        pendingInputs.push({
          message_id: requestId,
          content,
          target: "next-turn",
          source: "user",
          image_count: Array.isArray(payload.images) ? payload.images.length : 0,
          artifact_count: Array.isArray(payload.attachments) ? payload.attachments.length : 0,
        });
        publishRuntime([{ type: "queue_updated", data: { items: pendingInputs } }], requestId);
        return sse(route, []);
      }
      demoMessageCount += content.startsWith("Plan") ? 4 : content.startsWith("Write") ? 1 : 2;
      if (content.startsWith("Hold")) {
        await new Promise((resolve) => setTimeout(resolve, content.startsWith("Hold queue") ? 1600 : 400));
        const events = [
          { type: "turn_started", data: { turn: 2 } },
          { type: "assistant_message", data: { content: "The delayed turn completed.", tool_calls: [] } },
          { type: "turn_finished", data: { turn: 2, status_slots: { goal: "active" } } },
        ];
        publishRuntime(events, requestId);
        return sse(route, events);
      }
      if (content.startsWith("Write")) {
        const events = [{
          type: "permission_request",
          data: {
            request_id: "permission-1",
            source: "tool",
            reason: "Write the requested report",
            tool_call: { id: "call-write", name: "filesystem_write", args: { path: "report.md" } },
            decision: "ask",
            resume_supported: true,
          },
        }];
        publishRuntime(events, requestId);
        return sse(route, events);
      }
      if (content.startsWith("Plan")) {
        const todos = [
          { content: "Inspect the bug", status: "in_progress" },
          { content: "Verify the fix", status: "pending" },
        ];
        const current = [
          { content: "Inspect the bug", status: "completed" },
          { content: "Verify the fix", status: "in_progress" },
        ];
        const events = [
          { type: "turn_started", data: { turn: 2 } },
          { type: "tool_calls_started", data: { tool_calls: [{ id: "todo-1", name: "update_todos", args: { todos } }] } },
          { type: "tool_result", data: {
            tool_call_id: "todo-1",
            name: "update_todos",
            content: "Todo list updated.",
            status: "success",
            data: { kind: "todo_snapshot", schema_version: 1, items: current },
          } },
          { type: "assistant_message", data: { content: "I will work through the checklist.", tool_calls: [] } },
          { type: "turn_finished", data: { turn: 2, status_slots: { goal: "active" } } },
        ];
        publishRuntime(events, requestId);
        return sse(route, events);
      }
      if (content.startsWith("Long tool")) {
        const output = Array.from({ length: 40 }, (_, index) => `line-${index}`).join("\n");
        const events = [
          { type: "turn_started", data: { turn: 2 } },
          { type: "tool_calls_started", data: { tool_calls: [{ id: "shell-long", name: "shell", args: { command: "emit-lines" } }] } },
          { type: "tool_result", data: { tool_call_id: "shell-long", name: "shell", content: output, status: "success" } },
          { type: "assistant_message", data: { content: "Long output inspected.", tool_calls: [] } },
          { type: "turn_finished", data: { turn: 2, status_slots: { goal: "active" } } },
        ];
        publishRuntime(events, requestId);
        return sse(route, events);
      }
      const events = [
        { type: "turn_started", data: { turn: 2 } },
        { type: "assistant_message_delta", data: { reasoning: "I am checking the public resources." } },
        { type: "assistant_message_delta", data: { content: "The Web client remains behind the typed v3 API." } },
        { type: "assistant_message", data: { content: "The Web client remains behind the typed v3 API.", tool_calls: [] } },
        { type: "usage", data: { input_tokens: 280, output_tokens: 90, total_tokens: 370, requests: 1, context_tokens: 1200 } },
        { type: "turn_finished", data: { turn: 2, status_slots: { goal: "active" } } },
      ];
      publishRuntime(events, requestId);
      return sse(route, events);
    }
    if (path.endsWith("/interactions/permission-response")) return json(route, {
      request_id: "permission-1",
      recorded: true,
      pending_interactions: [],
    });
    return json(route, { code: "not_mocked", message: `${method} ${path}` }, 404);
  });
}

function openSession(sessionId = "demo-session", workspaceOverride = "", historyLimit = 0) {
  const historical = sessionId === "history-session";
  const longHistory = sessionId === "long-session";
  const blank = sessionId === "second-session";
  const history = blank ? [] : historical ? [
    { role: "user", content: "Review the persisted workspace", tool_calls: [], tool_call_id: "", status: "", data: null, error: null, artifacts: [] },
    { role: "assistant", content: "A persisted answer from history.", reasoning: "I checked the persisted context.", tool_calls: [], tool_call_id: "", status: "", data: null, error: null, artifacts: [] },
  ] : longHistory ? Array.from({ length: 100 }, (_, index) => [
    { role: "user", content: `Historical message ${index + 1}`, tool_calls: [], tool_call_id: "", status: "", data: null, error: null, artifacts: [] },
    { role: "assistant", content: `Historical answer ${index + 1}`, tool_calls: [], tool_call_id: "", status: "", data: null, error: null, artifacts: [] },
  ]).flat() : [
    { role: "user", content: "Inspect API boundaries", tool_calls: [], tool_call_id: "", status: "", data: null, error: null, artifacts: [] },
    { role: "assistant", content: "I will inspect the public SDK surface.", tool_calls: [{ id: "call-read", name: "filesystem_read", args: { path: "docs/sdk.md" } }], tool_call_id: "", status: "", data: null, error: null, artifacts: [] },
    { role: "tool", content: "Protocol v3 is the source contract.", tool_calls: [], tool_call_id: "call-read", status: "success", data: null, error: null, artifacts: [] },
  ];
  const start = historyLimit ? Math.max(0, history.length - historyLimit) : 0;
  return {
    session_id: sessionId,
    thread_id: historical ? "history-main" : longHistory ? "long-main" : "agent",
    status: "ready",
    agent_name: "default",
    workspace_root: workspaceOverride || (historical ? "/workspace/history" : longHistory ? "/workspace/long" : "/workspace/XBot"),
    provider: "minimax",
    model: "Minimax-M3",
    model_mode: "high",
    context_window: 32000,
    usage: usage(),
    status_slots: { goal: "active" },
    pending_inputs: [],
    history: history.slice(start).map((item) => ({ images: [], ...item })),
    history_cursor: start ? String(start) : null,
    event_cursor: 0,
  };
}

function usage() {
  return {
    input_tokens: 800,
    output_tokens: 200,
    total_tokens: 1000,
    requests: 3,
    context_tokens: 800,
    cache_read_input_tokens: 0,
    cache_creation_input_tokens: 0,
    prompt_cache_write_tokens: 0,
  };
}

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

function sse(route: Route, events: Array<{ type: string; data: Record<string, unknown> }>) {
  const envelopes = [...events, { type: "end", data: { status: "ok" } }];
  const body = envelopes.map((event, index) => {
    const payload = {
      protocol_version: "xbotv2.v3",
      session_id: "demo-session",
      thread_id: "agent",
      request_id: "request-1",
      sequence: index + 1,
      ...event,
    };
    return `event: ${event.type}\nid: ${index + 1}\ndata: ${JSON.stringify(payload)}\n\n`;
  }).join("");
  return route.fulfill({ status: 200, contentType: "text/event-stream", body });
}

function runtimeSse(
  route: Route,
  events: Array<{
    type: string;
    data: Record<string, unknown>;
    requestId: string;
    sequence: number;
  }>,
  after: number,
) {
  const body = [
    ...events.map((event) => ({
      protocol_version: "xbotv2.v3",
      session_id: "demo-session",
      thread_id: "agent",
      request_id: event.requestId,
      sequence: event.sequence,
      type: event.type,
      data: event.data,
    })),
    {
      protocol_version: "xbotv2.v3",
      session_id: "demo-session",
      thread_id: "agent",
      request_id: "",
      sequence: events.at(-1)?.sequence ?? after,
      type: "end",
      data: { status: "ok" },
    },
  ].map((event) => (
    `event: ${event.type}\nid: ${event.sequence}\ndata: ${JSON.stringify(event)}\n\n`
  )).join("");
  return route.fulfill({ status: 200, contentType: "text/event-stream", body });
}
