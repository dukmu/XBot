import { expect, test, type Page, type Route } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  await mockProtocol(page);
  await page.goto("/");
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

  await expect(page.getByText("Thinking", { exact: true })).toBeVisible();
  await page.getByText("Thinking", { exact: true }).click();
  await expect(page.getByText("I am checking the public resources.")).toBeVisible();
  await expect(page.getByText("filesystem_read", { exact: true })).toBeVisible();
  await expect(page.locator(".tool-details")).toHaveCount(0);
  await page.getByText("filesystem_read", { exact: true }).click();
  await expect(page.getByText("Arguments", { exact: true })).toBeVisible();
  await expect(page.getByText("Explorer", { exact: true })).toBeVisible();
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
  await expect(page.getByText("0/2 done · Inspect the bug", { exact: true })).toBeVisible();
  await page.getByText("update_todos", { exact: true }).click();
  await expect(page.getByRole("region", { name: "Todo checklist" })).toContainText("Inspect the bug");
  await expect(page.getByRole("region", { name: "Todo checklist" })).toContainText("In progress");
  await expect(page.getByRole("region", { name: "Todo checklist" })).toContainText("Pending");
});

test("restores historical sessions and their workspaces", async ({ page }) => {
  await openDemoSession(page);
  const mobile = (page.viewportSize()?.width || 0) <= 820;
  if (mobile) await page.getByRole("button", { name: "Open sessions" }).click();

  await page.getByTitle("history-session").click();
  if (mobile) {
    await expect(page.getByText("A persisted answer from history.", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Open sessions" }).click();
    await expect(page.getByRole("button", { name: /Historical review.*workspace\/history/ })).toBeVisible();
  } else {
    await expect(page.getByTitle("/workspace/history")).toBeVisible();
  }

  await page.getByTitle("demo-session").click();
  if (mobile) {
    await expect(page.getByText("Inspect API boundaries", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Open sessions" }).click();
    await expect(page.getByRole("button", { name: /Demo session.*workspace\/XBot/ })).toBeVisible();
  } else {
    await expect(page.getByTitle("/workspace/XBot")).toBeVisible();
  }
  await expect(page.getByText("Inspect API boundaries", { exact: true })).toBeVisible();
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
        model: "Minimax-M3",
        max_tokens: 32768,
        reasoning_effort: "high",
        thinking_enabled: true,
      }],
    });
    if (path === "/sessions" && method === "GET") return json(route, {
      sessions: [
        { session_id: "demo-session", title: "Demo session", workspace_root: "/workspace/XBot", status: "inactive", active_threads: 0, thread_count: 1 },
        { session_id: "history-session", title: "Historical review", workspace_root: "/workspace/history", status: "inactive", active_threads: 0, thread_count: 1 },
        { session_id: "long-session", title: "Long history", workspace_root: "/workspace/long", status: "inactive", active_threads: 0, thread_count: 1 },
      ],
    });
    if (path === "/sessions" && method === "POST") {
      const sessionId = String(request.postDataJSON().session_id || "demo-session");
      return json(route, openSession(sessionId));
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
        message_count: 3,
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
    if (path.endsWith("/agents")) return json(route, {
      active: "default",
      agents: [{ name: "default", description: "Primary Agent", mode: "primary", provider: "", model: "", context_window: 32000 }],
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
    if (path.endsWith("/events")) return sse(route, []);
    if (path.endsWith("/messages") && method === "POST") {
      const content = String(request.postDataJSON().content || "");
      if (content.startsWith("Write")) {
        return sse(route, [{
          type: "permission_request",
          data: {
            request_id: "permission-1",
            source: "tool",
            reason: "Write the requested report",
            tool_call: { id: "call-write", name: "filesystem_write", args: { path: "report.md" } },
            decision: "ask",
            resume_supported: true,
          },
        }]);
      }
      if (content.startsWith("Plan")) {
        const todos = [
          { content: "Inspect the bug", status: "in_progress" },
          { content: "Verify the fix", status: "pending" },
        ];
        return sse(route, [
          { type: "turn_started", data: { turn: 2 } },
          { type: "tool_calls_started", data: { tool_calls: [{ id: "todo-1", name: "update_todos", args: { todos } }] } },
          { type: "tool_result", data: { tool_call_id: "todo-1", name: "update_todos", content: "Todo list updated.", status: "success" } },
          { type: "assistant_message", data: { content: "I will work through the checklist.", tool_calls: [] } },
          { type: "turn_finished", data: { turn: 2, status_slots: { goal: "active" } } },
        ]);
      }
      return sse(route, [
        { type: "turn_started", data: { turn: 2 } },
        { type: "assistant_message_delta", data: { reasoning: "I am checking the public resources." } },
        { type: "assistant_message_delta", data: { content: "The Web client remains behind the typed v3 API." } },
        { type: "assistant_message", data: { content: "The Web client remains behind the typed v3 API.", tool_calls: [] } },
        { type: "usage", data: { input_tokens: 280, output_tokens: 90, total_tokens: 370, requests: 1, context_tokens: 1200 } },
        { type: "turn_finished", data: { turn: 2, status_slots: { goal: "active" } } },
      ]);
    }
    if (path.endsWith("/interactions/permission-response")) return json(route, {
      request_id: "permission-1",
      recorded: true,
      pending_interactions: [],
    });
    return json(route, { code: "not_mocked", message: `${method} ${path}` }, 404);
  });
}

function openSession(sessionId = "demo-session") {
  const historical = sessionId === "history-session";
  const longHistory = sessionId === "long-session";
  return {
    session_id: sessionId,
    thread_id: historical ? "history-main" : longHistory ? "long-main" : "agent",
    status: "ready",
    agent_name: "default",
    workspace_root: historical ? "/workspace/history" : longHistory ? "/workspace/long" : "/workspace/XBot",
    provider: "minimax",
    model: "Minimax-M3",
    model_mode: "high",
    context_window: 32000,
    usage: usage(),
    status_slots: { goal: "active" },
    history: historical ? [
      { role: "user", content: "Review the persisted workspace", tool_calls: [], tool_call_id: "", status: "", data: null, error: null, artifacts: [] },
      { role: "assistant", content: "A persisted answer from history.", tool_calls: [], tool_call_id: "", status: "", data: null, error: null, artifacts: [] },
    ] : longHistory ? Array.from({ length: 100 }, (_, index) => [
      { role: "user", content: `Historical message ${index + 1}`, tool_calls: [], tool_call_id: "", status: "", data: null, error: null, artifacts: [] },
      { role: "assistant", content: `Historical answer ${index + 1}`, tool_calls: [], tool_call_id: "", status: "", data: null, error: null, artifacts: [] },
    ]).flat() : [
      { role: "user", content: "Inspect API boundaries", tool_calls: [], tool_call_id: "", status: "", data: null, error: null, artifacts: [] },
      { role: "assistant", content: "I will inspect the public SDK surface.", tool_calls: [{ id: "call-read", name: "filesystem_read", args: { path: "docs/sdk.md" } }], tool_call_id: "", status: "", data: null, error: null, artifacts: [] },
      { role: "tool", content: "Protocol v3 is the source contract.", tool_calls: [], tool_call_id: "call-read", status: "success", data: null, error: null, artifacts: [] },
    ],
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
