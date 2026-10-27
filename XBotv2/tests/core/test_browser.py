from XBotv2.tests.runtime import engine_of
"""Focused behavior tests for the built-in Browser plugin."""

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
import pytest
from XBotv2.core.api.tools import ToolDenied, ToolFailed, ToolOutcome, ToolSucceeded
from XBotv2.agentloop.api.contracts import HumanInput, InboxItem
from XBotv2.core.api.domain import InboxTarget
from XBotv2.agentloop.api.outputs import AssistantCompleted, LoopError, ToolCompleted
from XBotv2.tests.runtime import thread_scope
from XBotv2.core.api.messages import ToolMessage
from XBotv2.core.api.paths import RuntimePaths
from XBotv2.llm.impl.mock import MockLLM
from XBotv2.permissions.api.models import Allowed, Denied, PermissionRequest


@pytest.fixture
def browser_approval_policy():
    """These workflows exercise approval, independently of shipped defaults."""
    return {
        "id": "permissions",
        "config": {"rules": [
            {
                "tool_pattern": "(?:browser_click|browser_fill|browser_press|browser_select)",
                "decision": "ask",
            },
            {
                "tool_pattern": "(?:browser_open|browser_snapshot|browser_screenshot|browser_close)",
                "decision": "allow",
            },
        ]},
    }


def _text(result: ToolOutcome) -> str:
    if isinstance(result, ToolFailed):
        return "".join(part.text for part in result.output.parts)
    assert isinstance(result, ToolSucceeded)
    return "".join(part.text for part in result.output.parts)


@pytest.mark.asyncio
async def test_agent_web_fetch_uses_registered_tool_and_active_sandbox_policy(
    tmp_path,
):
    """The public Agent tool path denies fetch before opening a network client."""
    llm = MockLLM(responses=[
        {"tool_calls": [{
            "id": "fetch-private-network",
            "name": "web_fetch",
            "args": {"url": "https://example.com/"},
        }]},
        {"content": "The network is disabled."},
    ])
    application = await thread_scope(
        paths=RuntimePaths.from_data_dir(tmp_path / "data"),
        session_id="browser-policy-e2e",
        thread_id="main",
        workspace_root=tmp_path / "workspace",
        llm_override=llm,
        extra_plugins=[
            {
                "id": "sandbox",
                "config": {"network": False},
            },
            {
                "id": "caption",
                "config": {"auto": False, "allow_access": False},
            },
        ],
    )

    try:
        events = [event async for event in engine_of(application).run_turn(InboxItem(
            target=InboxTarget.NEXT_TURN,
            input=HumanInput(content="Fetch this page."),
        ))]
        tool_request = llm.request_history[0]
        completed_tools = [
            event for event in events if isinstance(event, ToolCompleted)
        ]
        assert completed_tools, (
            [event.kind for event in events],
            llm.request_history,
        )
        tool_outcome = completed_tools[0].execution.message.outcome
        final_request = llm.request_history[-1]
        history = application.loop_state.history.snapshot()
    finally:
        await application.stop()

    assert "web_fetch" in {tool.name for tool in tool_request.tools}
    assert "web_fetch" in {tool.name for tool in final_request.tools}
    assert isinstance(tool_outcome, ToolFailed)
    assert tool_outcome.error.code == "network_disabled"
    tool_message = next(
        message for message in history if isinstance(message, ToolMessage)
    )
    assert tool_message.outcome == tool_outcome
    assert not [event for event in events if isinstance(event, LoopError)]
    assert any(isinstance(event, AssistantCompleted) for event in events)


@pytest.mark.asyncio
async def test_agent_web_fetch_reads_local_page_and_closes_network_client(
    tmp_path, monkeypatch
):
    """The registered tool, sandbox, HTTP client, history, and app cleanup compose."""
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"<html><main><h1>Local evidence</h1><p>Fetched through XBot.</p></main></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    clients = []
    real_web_access = __import__("XBotv2.browser.impl.plugin", fromlist=["WebAccess"]).WebAccess

    class TrackedWebAccess(real_web_access):
        def __init__(self, options):
            super().__init__(options)
            clients.append(self)

    monkeypatch.setattr("XBotv2.browser.impl.plugin.WebAccess", TrackedWebAccess)
    llm = MockLLM(responses=[
        {"tool_calls": [{
            "id": "fetch-local-page",
            "name": "web_fetch",
            "args": {"url": f"http://127.0.0.1:{server.server_port}/article"},
        }]},
        {"content": "The page says Local evidence."},
    ])
    application = await thread_scope(
        paths=RuntimePaths.from_data_dir(tmp_path / "data"),
        session_id="browser-fetch-e2e",
        thread_id="main",
        workspace_root=tmp_path / "workspace",
        llm_override=llm,
        extra_plugins=[
            {"id": "sandbox", "config": {"network": True}},
            {
                "id": "browser",
                "config": {"network": {"private_access": True}},
            },
            {
                "id": "caption",
                "config": {"auto": False, "allow_access": False},
            },
        ],
    )

    try:
        events = [event async for event in engine_of(application).run_turn(InboxItem(
            target=InboxTarget.NEXT_TURN,
            input=HumanInput(content="Fetch the local article."),
        ))]
        tool_request = llm.request_history[0]
        completed = next(event for event in events if isinstance(event, ToolCompleted))
        outcome = completed.execution.message.outcome
        history = application.loop_state.history.snapshot()
        assert isinstance(outcome, ToolSucceeded), _text(outcome)
        assert "Local evidence" in _text(outcome)
        assert any(isinstance(event, AssistantCompleted) for event in events)
        assert "web_fetch" in {tool.name for tool in tool_request.tools}
        assert next(
            message for message in history if isinstance(message, ToolMessage)
        ).outcome == outcome
        assert len(clients) == 1
        assert not clients[0]._client.is_closed
    finally:
        await application.stop()
        server.shutdown()
        thread.join()
        server.server_close()

    assert clients[0]._client.is_closed


@pytest.mark.asyncio
async def test_agent_browser_tools_require_permission_and_close_chromium_on_stop(
    tmp_path, browser_approval_policy,
):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"""<!doctype html>
            <html><head><title>Tool Browser</title></head><body>
              <input aria-label="Query">
              <select aria-label="Action">
                <option value="read">Read</option>
                <option value="summarize">Summarize</option>
              </select>
              <button onclick="document.querySelector('#result').textContent =
                document.querySelector('input').value + ':' +
                document.querySelector('select').value">Run</button>
              <p id="result">Waiting</p>
            </body></html>"""
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    llm = MockLLM(responses=[
        {"tool_calls": [{
            "id": "browser-open-local",
            "name": "browser_open",
            "args": {"url": f"http://127.0.0.1:{server.server_port}/form"},
        }]},
        {"tool_calls": [{
            "id": "browser-fill-local",
            "name": "browser_fill",
            "args": {"ref": "e1", "text": "through Agent tool"},
        }]},
        {"tool_calls": [{
            "id": "browser-select-local",
            "name": "browser_select",
            "args": {"ref": "e2", "value": "summarize"},
        }]},
        {"tool_calls": [{
            "id": "browser-click-local",
            "name": "browser_click",
            "args": {"ref": "e3"},
        }]},
        {"tool_calls": [{
            "id": "browser-screenshot-local",
            "name": "browser_screenshot",
            "args": {},
        }]},
        {"content": "The local browser workflow completed."},
    ])
    application = await thread_scope(
        paths=RuntimePaths.from_data_dir(tmp_path / "data"),
        session_id="browser-agent-tool-e2e",
        thread_id="main",
        workspace_root=tmp_path / "workspace",
        llm_override=llm,
        extra_plugins=[
            browser_approval_policy,
            {"id": "sandbox", "config": {"network": True}},
            {
                "id": "browser",
                "config": {"network": {"private_access": True}},
            },
            {"id": "caption", "config": {"auto": False, "allow_access": False}},
        ],
    )
    permission_requests = []

    async def approve_once(request):
        assert isinstance(request, PermissionRequest)
        permission_requests.append(request)
        return Allowed(scope="once")

    dispose_sink = application.client_events.install(approve_once)
    browser_tool = engine_of(application).tools.resolve("browser_open")
    assert browser_tool is not None
    browser_plugin = browser_tool.function.__self__
    try:
        events = [event async for event in engine_of(application).run_turn(InboxItem(
            target=InboxTarget.NEXT_TURN,
            input=HumanInput(content="Use the browser on the local form."),
        ))]
        completed = [event for event in events if isinstance(event, ToolCompleted)]
        messages = [event.execution.message for event in completed]
        assert [message.call.name for message in messages] == [
            "browser_open",
            "browser_fill",
            "browser_select",
            "browser_click",
            "browser_screenshot",
        ]
        assert all(isinstance(message.outcome, ToolSucceeded) for message in messages), [
            (
                message.call.name,
                getattr(getattr(message.outcome, "error", None), "message", ""),
            )
            for message in messages
        ]
        assert "through Agent tool:summarize" in _text(messages[3].outcome)
        assert len(messages[4].outcome.output.artifacts) == 1
        assert [request.subject.tool_call.name for request in permission_requests] == [
            "browser_fill",
            "browser_select",
            "browser_click",
        ]
        assert browser_plugin.diagnostics()["browser_active"] is True
        assert any(isinstance(event, AssistantCompleted) for event in events)
        history = application.loop_state.history.snapshot()
        assert [
            message.call.name
            for message in history
            if isinstance(message, ToolMessage)
        ] == [message.call.name for message in messages]
    finally:
        dispose_sink()
        await application.stop()
        server.shutdown()
        thread.join()
        server.server_close()

    assert browser_plugin.diagnostics()["browser_active"] is False


@pytest.mark.asyncio
async def test_agent_browser_permission_denial_prevents_page_mutation(tmp_path, browser_approval_policy):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    page = workspace / "form.html"
    page.write_text(
        """<!doctype html><title>Permission form</title>
        <input aria-label="Query">""",
        encoding="utf-8",
    )
    llm = MockLLM(responses=[
        {"tool_calls": [{
            "id": "open-sandbox-file",
            "name": "browser_open",
            "args": {"url": page.as_uri()},
        }]},
        {"tool_calls": [{
            "id": "denied-browser-fill",
            "name": "browser_fill",
            "args": {"ref": "e1", "text": "must not be entered"},
        }]},
        {"tool_calls": [{
            "id": "snapshot-after-denial",
            "name": "browser_snapshot",
            "args": {},
        }]},
        {"content": "The browser action was denied."},
    ])
    application = await thread_scope(
        paths=RuntimePaths.from_data_dir(tmp_path / "data"),
        session_id="browser-permission-denial-e2e",
        thread_id="main",
        workspace_root=workspace,
        llm_override=llm,
        extra_plugins=[
            browser_approval_policy,
            {"id": "sandbox", "config": {"network": True}},
            {"id": "caption", "config": {"auto": False, "allow_access": False}},
        ],
    )
    permission_requests = []

    async def deny(request):
        assert isinstance(request, PermissionRequest)
        permission_requests.append(request)
        return Denied(reason="Action declined by test client")

    dispose_sink = application.client_events.install(deny)
    browser_tool = engine_of(application).tools.resolve("browser_open")
    assert browser_tool is not None
    browser_plugin = browser_tool.function.__self__
    try:
        events = [event async for event in engine_of(application).run_turn(InboxItem(
            target=InboxTarget.NEXT_TURN,
            input=HumanInput(content="Open the local form, but deny editing it."),
        ))]
        messages = [
            event.execution.message
            for event in events
            if isinstance(event, ToolCompleted)
        ]
        assert [message.call.name for message in messages] == [
            "browser_open",
            "browser_fill",
            "browser_snapshot",
        ]
        assert isinstance(messages[0].outcome, ToolSucceeded)
        assert isinstance(messages[1].outcome, ToolDenied)
        assert isinstance(messages[2].outcome, ToolSucceeded)
        assert "input: Query" in _text(messages[2].outcome)
        assert "must not be entered" not in _text(messages[2].outcome)
        assert [request.subject.tool_call.name for request in permission_requests] == [
            "browser_fill",
        ]
        history = application.loop_state.history.snapshot()
        fill_record = next(
            message
            for message in history
            if isinstance(message, ToolMessage)
            and message.call.name == "browser_fill"
        )
        assert isinstance(fill_record.outcome, ToolDenied)
        assert browser_plugin.diagnostics()["browser_active"] is True
    finally:
        dispose_sink()
        await application.stop()

    assert browser_plugin.diagnostics()["browser_active"] is False


@pytest.mark.asyncio
async def test_agent_browser_permission_cancellation_prevents_page_mutation(
    tmp_path, browser_approval_policy,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    page = workspace / "form.html"
    page.write_text(
        """<!doctype html><title>Cancellation form</title>
        <input aria-label="Query">""",
        encoding="utf-8",
    )
    llm = MockLLM(responses=[
        {"tool_calls": [{
            "id": "open-before-cancel",
            "name": "browser_open",
            "args": {"url": page.as_uri()},
        }]},
        {"tool_calls": [{
            "id": "cancelled-browser-fill",
            "name": "browser_fill",
            "args": {"ref": "e1", "text": "must not be entered"},
        }]},
        {"tool_calls": [{
            "id": "snapshot-after-cancel",
            "name": "browser_snapshot",
            "args": {},
        }]},
        {"content": "The browser action was cancelled."},
    ])
    application = await thread_scope(
        paths=RuntimePaths.from_data_dir(tmp_path / "data"),
        session_id="browser-permission-cancel-e2e",
        thread_id="main",
        workspace_root=workspace,
        llm_override=llm,
        extra_plugins=[
            browser_approval_policy,
            {"id": "sandbox", "config": {"network": True}},
            {"id": "caption", "config": {"auto": False, "allow_access": False}},
        ],
    )
    router = application.client_events
    pending_requests = asyncio.Queue()

    async def wait_for_client_resolution(request):
        waiter = router.waiter_for(request)
        pending = waiter.register(request.interaction_id)
        pending_requests.put_nowait(request)
        return await waiter.wait_registered(
            request.interaction_id,
            pending,
            router.timeout_for(request),
        )

    dispose_sink = router.install(wait_for_client_resolution)
    browser_tool = engine_of(application).tools.resolve("browser_open")
    assert browser_tool is not None
    browser_plugin = browser_tool.function.__self__

    async def run_turn():
        return [event async for event in engine_of(application).run_turn(InboxItem(
            target=InboxTarget.NEXT_TURN,
            input=HumanInput(content="Open the form and wait before editing."),
        ))]

    try:
        turn = asyncio.create_task(run_turn())
        request = await asyncio.wait_for(pending_requests.get(), timeout=5)
        assert isinstance(request, PermissionRequest)
        assert request.subject.tool_call.name == "browser_fill"
        assert request.interaction_id in router.pending_request_ids()

        receipt = router.cancel(
            request.interaction_id,
            "cancelled by test client",
            expected_kind="permission_request",
        )
        assert isinstance(receipt.resolution, Denied)
        assert not router.pending_request_ids()
        events = await asyncio.wait_for(turn, timeout=5)
        messages = [
            event.execution.message
            for event in events
            if isinstance(event, ToolCompleted)
        ]
        assert [message.call.name for message in messages] == [
            "browser_open",
            "browser_fill",
            "browser_snapshot",
        ]
        assert isinstance(messages[1].outcome, ToolDenied)
        assert "input: Query" in _text(messages[2].outcome)
        assert "must not be entered" not in _text(messages[2].outcome)
        history = application.loop_state.history.snapshot()
        fill_record = next(
            message
            for message in history
            if isinstance(message, ToolMessage)
            and message.call.name == "browser_fill"
        )
        assert isinstance(fill_record.outcome, ToolDenied)
        assert browser_plugin.diagnostics()["browser_active"] is True
    finally:
        dispose_sink()
        await application.stop()

    assert browser_plugin.diagnostics()["browser_active"] is False


@pytest.mark.asyncio
async def test_agent_browser_blocks_loopback_before_launching_chromium(tmp_path):
    llm = MockLLM(responses=[
        {"tool_calls": [{
            "id": "blocked-browser-loopback",
            "name": "browser_open",
            "args": {"url": "http://127.0.0.1:9/private"},
        }]},
        {"content": "Private destinations are blocked."},
    ])
    application = await thread_scope(
        paths=RuntimePaths.from_data_dir(tmp_path / "data"),
        session_id="browser-private-destination-e2e",
        thread_id="main",
        workspace_root=tmp_path / "workspace",
        llm_override=llm,
        extra_plugins=[
            {"id": "sandbox", "config": {"network": True}},
            {"id": "caption", "config": {"auto": False, "allow_access": False}},
        ],
    )
    browser_tool = engine_of(application).tools.resolve("browser_open")
    assert browser_tool is not None
    browser_plugin = browser_tool.function.__self__
    try:
        events = [event async for event in engine_of(application).run_turn(InboxItem(
            target=InboxTarget.NEXT_TURN,
            input=HumanInput(content="Open this private destination."),
        ))]
        completed = next(event for event in events if isinstance(event, ToolCompleted))
        outcome = completed.execution.message.outcome
        assert isinstance(outcome, ToolFailed)
        assert outcome.error.code == "browser_open_failed"
        assert "Private, local, or non-routable" in outcome.error.message
        assert browser_plugin.diagnostics()["browser_active"] is False
        assert any(isinstance(event, AssistantCompleted) for event in events)
    finally:
        await application.stop()


@pytest.mark.asyncio
async def test_agent_web_search_uses_registered_tool_and_active_sandbox(
    tmp_path, monkeypatch
):
    class FakeDDGS:
        def __init__(self, timeout):
            self.timeout = timeout

        def text(self, query, **kwargs):
            assert query == "XBot runtime"
            assert kwargs["max_results"] == 5
            return [{
                "title": "XBot runtime",
                "href": "https://example.invalid/xbot",
                "body": "A readable agent runtime.",
            }]

    monkeypatch.setattr("ddgs.DDGS", FakeDDGS)
    llm = MockLLM(responses=[
        {"tool_calls": [{
            "id": "search-xbot",
            "name": "web_search",
            "args": {"query": "XBot runtime"},
        }]},
        {"content": "I found a result for XBot runtime."},
    ])
    application = await thread_scope(
        paths=RuntimePaths.from_data_dir(tmp_path / "data"),
        session_id="browser-search-e2e",
        thread_id="main",
        workspace_root=tmp_path / "workspace",
        llm_override=llm,
        extra_plugins=[
            {"id": "sandbox", "config": {"network": True}},
            {
                "id": "caption",
                "config": {"auto": False, "allow_access": False},
            },
        ],
    )

    try:
        events = [event async for event in engine_of(application).run_turn(InboxItem(
            target=InboxTarget.NEXT_TURN,
            input=HumanInput(content="Search for XBot runtime."),
        ))]
        completed = next(event for event in events if isinstance(event, ToolCompleted))
        outcome = completed.execution.message.outcome
        history = application.loop_state.history.snapshot()
        assert isinstance(outcome, ToolSucceeded), _text(outcome)
        assert "A readable agent runtime." in _text(outcome)
        assert "https://example.invalid/xbot" in _text(outcome)
        assert "web_search" in {tool.name for tool in llm.request_history[0].tools}
        assert next(
            message for message in history if isinstance(message, ToolMessage)
        ).outcome == outcome
        assert any(isinstance(event, AssistantCompleted) for event in events)
    finally:
        await application.stop()
