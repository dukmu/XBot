"""Focused behavior tests for the built-in Browser plugin."""

import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
import pytest
from XBotv2.browser.impl.browser import BrowserSession
from XBotv2.browser.api.models import BrowserSessionPolicy, NetworkPolicy
from XBotv2.browser.impl.network import BrowserProxy, WebAccess, validate_url
from XBotv2.core.api.artifacts import ArtifactKind
from XBotv2.core.api.tools import ToolDenied, ToolFailed, ToolOutcome, ToolSucceeded, succeeded_text
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


class FakeBrowserSandbox:
    network = True

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace

    def resolve_read_path(self, path: str) -> Path:
        candidate = Path(path)
        return candidate if candidate.is_absolute() else self.workspace / candidate

    def check_filesystem_access(self, _operation, args):
        path = Path(str(args["path"]))
        if path.is_relative_to(self.workspace):
            return []
        return [{"field": "path", "path": str(path), "write": False, "decision": "deny"}]


class NoNetworkSandbox(FakeBrowserSandbox):
    network = False
















@pytest.mark.asyncio
async def test_browser_open_http_uses_unified_network_guard(tmp_path, artifact_store):
    browser = BrowserSession(
        network_policy=NetworkPolicy(),
        session_policy=BrowserSessionPolicy(timeout=5),
        artifacts=artifact_store,
        sandbox=NoNetworkSandbox(tmp_path),
    )

    result = await browser.open("https://example.com/page")

    assert isinstance(result, ToolFailed)
    assert result.error.code == "network_disabled"


@pytest.mark.asyncio
async def test_browser_route_blocks_remote_subresources_when_sandbox_network_is_off(
    tmp_path, artifact_store
):
    class Route:
        action = ""

        async def abort(self, _reason):
            self.action = "abort"

        async def continue_(self):
            self.action = "continue"

    browser = BrowserSession(
        network_policy=NetworkPolicy(private_access=True),
        session_policy=BrowserSessionPolicy(timeout=5),
        artifacts=artifact_store,
        sandbox=NoNetworkSandbox(tmp_path),
    )
    route = Route()

    await browser._guard_request(
        route,
        SimpleNamespace(url="https://93.184.216.34/remote-image.png"),
    )

    assert route.action == "abort"


@pytest.mark.asyncio
async def test_real_chromium_interaction_and_screenshot_artifact_lifecycle(
    tmp_path, artifact_store
):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"""<!doctype html>
            <html><head><title>Local Browser Test</title></head><body>
              <label>Query <input aria-label="Query"></label>
              <label>Action <select aria-label="Action">
                <option value="read">Read</option>
                <option value="summarize">Summarize</option>
              </select></label>
              <button type="button" onclick="document.querySelector('#result').textContent =
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
    browser = BrowserSession(
        network_policy=NetworkPolicy(private_access=True),
        session_policy=BrowserSessionPolicy(timeout=10),
        artifacts=artifact_store,
        sandbox=FakeBrowserSandbox(tmp_path),
    )

    try:
        opened = await browser.open(
            f"http://127.0.0.1:{server.server_port}/form"
        )
        assert isinstance(opened, ToolSucceeded), _text(opened)
        assert "Title: Local Browser Test" in _text(opened)
        assert "[e1] input: Query" in _text(opened)
        assert "[e2] select: Read" in _text(opened)
        assert "[e3] button: Run" in _text(opened)

        filled = await browser.fill("e1", "runtime behavior")
        assert isinstance(filled, ToolSucceeded), _text(filled)
        assert "input: runtime behavior" in _text(filled)

        selected = await browser.select("e2", "summarize")
        assert isinstance(selected, ToolSucceeded), _text(selected)
        assert "[e2] select: Summarize" in _text(selected)

        clicked = await browser.click("e3")
        assert isinstance(clicked, ToolSucceeded), _text(clicked)
        assert "runtime behavior:summarize" in _text(clicked)

        screenshot = await browser.screenshot()
        assert isinstance(screenshot, ToolSucceeded), _text(screenshot)
        assert len(screenshot.output.artifacts) == 1
        artifact = screenshot.output.artifacts[0]
        assert artifact.kind is ArtifactKind.BROWSER
        assert artifact.media_type == "image/png"
        assert artifact.name.endswith(".png")

        closed = await browser.close()
        assert isinstance(closed, ToolSucceeded), _text(closed)
        assert not browser.active

        assert (await artifact_store.exists(artifact))
        payload = (await artifact_store.read(artifact))
        assert len(payload) == artifact.size
        assert payload.startswith(b"\x89PNG\r\n\x1a\n")
    finally:
        await browser.shutdown()
        server.shutdown()
        thread.join()
        server.server_close()


@pytest.mark.asyncio
async def test_real_chromium_blocks_private_subresource_from_sandboxed_file(
    tmp_path, artifact_store
):
    requested_paths = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requested_paths.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", "9")
            self.end_headers()
            self.wfile.write(b"not a png")

        def log_message(self, _format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    page = tmp_path / "hostile.html"
    page.write_text(
        f"""<!doctype html><title>Hostile page</title>
        <img src="http://127.0.0.1:{server.server_port}/private.png"
          onerror="document.body.dataset.privateRequestBlocked='yes'">""",
        encoding="utf-8",
    )
    browser = BrowserSession(
        network_policy=NetworkPolicy(),
        session_policy=BrowserSessionPolicy(timeout=10),
        artifacts=artifact_store,
        sandbox=FakeBrowserSandbox(tmp_path),
    )

    try:
        opened = await browser.open(page.as_uri())
        assert isinstance(opened, ToolSucceeded), _text(opened)
        await browser._page.wait_for_function(
            "document.body.dataset.privateRequestBlocked === 'yes'",
            timeout=5000,
        )
        assert requested_paths == []
    finally:
        await browser.shutdown()
        server.shutdown()
        thread.join()
        server.server_close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_resource", ["context", "browser", "playwright"])
async def test_browser_close_reports_failure_after_attempting_all_resource_cleanup(
    tmp_path, artifact_store, failed_resource
):
    attempted = []

    class Resource:
        def __init__(self, name, *, fails=False):
            self.name = name
            self.fails = fails

        async def close(self):
            attempted.append(self.name)
            if self.fails:
                raise RuntimeError(f"{self.name} close failed")

    class Playwright:
        async def stop(self):
            attempted.append("playwright")
            if failed_resource == "playwright":
                raise RuntimeError("playwright stop failed")

    class Page:
        def is_closed(self):
            return False

    browser = BrowserSession(
        network_policy=NetworkPolicy(),
        session_policy=BrowserSessionPolicy(timeout=5),
        artifacts=artifact_store,
        sandbox=FakeBrowserSandbox(tmp_path),
    )
    browser._page = Page()
    browser._context = Resource("context", fails=failed_resource == "context")
    browser._browser = Resource("browser", fails=failed_resource == "browser")
    browser._playwright = Playwright()

    result = await browser.close()

    assert isinstance(result, ToolFailed)
    assert result.error.code == "browser_close_failed"
    expected_failure = {
        "context": "context close failed",
        "browser": "browser close failed",
        "playwright": "playwright stop failed",
    }[failed_resource]
    assert expected_failure in result.error.message
    assert attempted == ["context", "browser", "playwright"]
    assert not browser.active


@pytest.mark.asyncio
async def test_browser_actions_call_their_typed_locator_operations(
    tmp_path, artifact_store
):
    actions = []

    class Locator:
        async def count(self):
            return 1

        async def click(self):
            actions.append(("click",))

        async def fill(self, text):
            actions.append(("fill", text))

        async def select_option(self, value):
            actions.append(("select_option", value))

    class Page:
        def locator(self, selector):
            assert selector == '[data-xbot-ref="e1"]'
            return Locator()

        def is_closed(self):
            return False

        async def wait_for_timeout(self, _timeout):
            return None

    class InteractiveBrowser(BrowserSession):
        def __init__(self):
            super().__init__(
                network_policy=NetworkPolicy(),
                session_policy=BrowserSessionPolicy(timeout=5),
                artifacts=artifact_store,
                sandbox=FakeBrowserSandbox(tmp_path),
            )
            self._page = Page()

        async def snapshot(self):
            return succeeded_text("updated page")

    browser = InteractiveBrowser()

    click = await browser.click("e1")
    fill = await browser.fill("e1", "XBot")
    select = await browser.select("e1", "agent")

    assert [click, fill, select] == [
        succeeded_text("updated page"),
        succeeded_text("updated page"),
        succeeded_text("updated page"),
    ]
    assert actions == [
        ("click",),
        ("fill", "XBot"),
        ("select_option", "agent"),
    ]


@pytest.mark.asyncio
async def test_browser_actions_reject_invalid_and_stale_refs(
    tmp_path, artifact_store
):
    class Locator:
        async def count(self):
            return 0

    class Page:
        def locator(self, _selector):
            return Locator()

        def is_closed(self):
            return False

    class InteractiveBrowser(BrowserSession):
        def __init__(self):
            super().__init__(
                network_policy=NetworkPolicy(),
                session_policy=BrowserSessionPolicy(timeout=5),
                artifacts=artifact_store,
                sandbox=FakeBrowserSandbox(tmp_path),
            )
            self._page = Page()

    browser = InteractiveBrowser()

    invalid = await browser.click("not a ref")
    stale = await browser.fill("e9", "XBot")

    assert isinstance(invalid, ToolFailed)
    assert invalid.error.code == "invalid_ref"
    assert isinstance(stale, ToolFailed)
    assert stale.error.code == "stale_ref"


@pytest.mark.asyncio
async def test_web_search_normalizes_ddgs_results(monkeypatch):
    class FakeDDGS:
        def __init__(self, timeout):
            self.timeout = timeout
            self.kwargs = {}

        def text(self, query, **kwargs):
            self.kwargs = {"query": query, **kwargs}
            return [
                {
                    "title": "XBot project",
                    "href": "https://example.com/xbot",
                    "body": "Readable Agent runtime",
                    "date": "2026-07-29",
                },
                {"title": "No URL", "body": "skipped"},
            ]

    fake = FakeDDGS(timeout=20)
    monkeypatch.setattr("ddgs.DDGS", lambda timeout: fake)
    access = WebAccess(NetworkPolicy())
    try:
        result = await access.search(
            "xbot",
            max_results=5,
            freshness="week",
            backend="auto",
            region="wt-wt",
            safesearch="moderate",
        )
    finally:
        await access.close()

    assert isinstance(result, ToolSucceeded)
    # The data is now embedded in content as JSON
    payload = json.loads(_text(result).split("\n\n")[1])
    assert payload["results"] == [{
        "title": "XBot project",
        "url": "https://example.com/xbot",
        "snippet": "Readable Agent runtime",
        "date": "2026-07-29",
    }]
    assert fake.kwargs["query"] == "xbot"
    assert fake.kwargs["max_results"] == 5
    assert fake.kwargs["timelimit"] == "w"


@pytest.mark.asyncio
async def test_web_search_reports_ddgs_failure(monkeypatch):
    class FailingDDGS:
        def __init__(self, timeout):
            del timeout

        def text(self, query, **kwargs):
            raise RuntimeError("search backend unavailable")

    monkeypatch.setattr("ddgs.DDGS", FailingDDGS)
    access = WebAccess(NetworkPolicy())
    try:
        result = await access.search(
            "xbot",
            max_results=3,
            freshness=None,
            backend="auto",
            region="wt-wt",
            safesearch="off",
        )
    finally:
        await access.close()

    assert isinstance(result, ToolFailed)
    assert result.error.code == "search_failed"


@pytest.mark.asyncio
async def test_browser_file_url_resolves_inside_sandbox(tmp_path, artifact_store):
    page = tmp_path / "page.html"
    page.write_text("<h1>local</h1>", encoding="utf-8")
    sandbox = FakeBrowserSandbox(tmp_path)
    browser = BrowserSession(
        network_policy=NetworkPolicy(),
        session_policy=BrowserSessionPolicy(timeout=5),
        artifacts=artifact_store,
        sandbox=sandbox,
    )

    target = await browser._target_url(page.as_uri())

    assert target == "file://" + str(page)


@pytest.mark.asyncio
async def test_browser_file_url_rejects_path_outside_sandbox(
    tmp_path, artifact_store
):
    outside = tmp_path.parent / "outside.html"
    sandbox = FakeBrowserSandbox(tmp_path)
    browser = BrowserSession(
        network_policy=NetworkPolicy(),
        session_policy=BrowserSessionPolicy(timeout=5),
        artifacts=artifact_store,
        sandbox=sandbox,
    )

    with pytest.raises(ValueError, match="outside the sandbox"):
        await browser._target_url(outside.as_uri())


@pytest.mark.asyncio
async def test_browser_open_accepts_file_url_with_sandbox(tmp_path, artifact_store):
    class FakeBrowser(BrowserSession):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.opened = ""

        async def _ensure_page(self):
            return self

        async def goto(self, url, **_kwargs):
            self.opened = url

        async def snapshot(self):
            return succeeded_text("snapshot")

    page = tmp_path / "page.html"
    page.write_text("<h1>local</h1>", encoding="utf-8")
    browser = FakeBrowser(
        network_policy=NetworkPolicy(),
        session_policy=BrowserSessionPolicy(timeout=5),
        artifacts=artifact_store,
        sandbox=FakeBrowserSandbox(tmp_path),
    )

    result = await browser.open(page.as_uri())

    assert isinstance(result, ToolSucceeded)
    assert browser.opened == "file://" + str(page)


@pytest.mark.asyncio
async def test_url_policy_blocks_private_destinations():
    with pytest.raises(ValueError, match="Private, local"):
        await validate_url("http://127.0.0.1/private", NetworkPolicy())

    checked = await validate_url("https://93.184.216.34/page", NetworkPolicy())
    assert checked.request_url == "https://93.184.216.34/page"
    assert checked.connection_url == checked.request_url


@pytest.mark.parametrize(
    "url",
    [
        "http://10.0.0.1/metadata",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/admin",
        "http://[fe80::1]/admin",
        "http://[fc00::1]/admin",
    ],
)
@pytest.mark.asyncio
async def test_url_policy_blocks_private_ipv4_and_ipv6_literals(url):
    with pytest.raises(ValueError, match="Private, local, or non-routable"):
        await validate_url(url, NetworkPolicy())


@pytest.mark.asyncio
async def test_url_policy_rejects_hostname_with_mixed_public_and_private_dns(
    monkeypatch,
):
    import ipaddress

    monkeypatch.setattr(
        "XBotv2.browser.impl.network._resolve_addresses",
        lambda _hostname: {
            ipaddress.ip_address("93.184.216.34"),
            ipaddress.ip_address("10.0.0.7"),
        },
    )

    with pytest.raises(ValueError, match="Private, local, or non-routable"):
        await validate_url("https://mixed-answer.example/", NetworkPolicy())


@pytest.mark.asyncio
async def test_url_policy_pins_public_address_and_preserves_authority(monkeypatch):
    import ipaddress

    monkeypatch.setattr(
        "XBotv2.browser.impl.network._resolve_addresses",
        lambda _hostname: {ipaddress.ip_address("93.184.216.34")},
    )

    checked = await validate_url(
        "https://public.example:8443/article?q=x",
        NetworkPolicy(),
    )

    assert checked.request_url == "https://public.example:8443/article?q=x"
    assert checked.connection_url == "https://93.184.216.34:8443/article?q=x"
    assert checked.host_header == "public.example:8443"
    assert checked.tls_server_name == "public.example"


@pytest.mark.asyncio
async def test_web_fetch_extracts_readable_html():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = (
                b"<html><head><title>Example article</title></head>"
                b"<body><main><h1>Release notes</h1>"
                b"<p>The browser plugin fetched this content.</p></main></body></html>"
            )
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
    access = WebAccess(NetworkPolicy(private_access=True))
    try:
        result = await access.fetch(
            f"http://127.0.0.1:{server.server_port}/article"
        )
    finally:
        await access.close()
        server.shutdown()
        thread.join()
        server.server_close()

    assert isinstance(result, ToolSucceeded)
    assert "Release notes" in _text(result)
    # The metadata is embedded as JSON after the content
    payload = json.loads(_text(result).split("\n\n")[-1])
    assert payload["content_type"] == "text/html"
    assert payload["url"].endswith("/article")
    assert payload["untrusted"] is True


@pytest.mark.asyncio
async def test_web_fetch_follows_redirects_and_limits_response_size():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/final")
                self.end_headers()
                return
            body = b"redirect complete" if self.path == "/final" else b"x" * 128
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    access = WebAccess(NetworkPolicy(max_bytes=64, private_access=True))
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        redirected = await access.fetch(f"{base_url}/redirect")
        oversized = await access.fetch(f"{base_url}/large")
    finally:
        await access.close()
        server.shutdown()
        thread.join()
        server.server_close()

    assert isinstance(redirected, ToolSucceeded)
    payload = json.loads(_text(redirected).split("\n\n")[-1])
    assert payload["url"].endswith("/final")
    assert "redirect complete" in _text(redirected)
    assert isinstance(oversized, ToolFailed)
    assert oversized.error.code == "fetch_failed"
    assert "size limit" in oversized.error.message


@pytest.mark.asyncio
async def test_web_fetch_rejects_redirect_to_private_destination(monkeypatch):
    import ipaddress
    import httpx

    requests = []

    async def respond(request):
        requests.append((
            str(request.url),
            request.headers["host"],
            request.extensions.get("sni_hostname"),
        ))
        return httpx.Response(
            302,
            headers={"Location": "http://127.0.0.1/private"},
        )

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        "XBotv2.browser.impl.network._resolve_addresses",
        lambda _hostname: {ipaddress.ip_address("93.184.216.34")},
    )
    monkeypatch.setattr(
        "XBotv2.browser.impl.network.httpx.AsyncClient",
        lambda **kwargs: real_async_client(
            transport=httpx.MockTransport(respond),
            **kwargs,
        ),
    )
    access = WebAccess(NetworkPolicy())
    try:
        result = await access.fetch("https://public.example/redirect")
    finally:
        await access.close()

    assert isinstance(result, ToolFailed)
    assert result.error.code == "fetch_failed"
    assert "Private, local, or non-routable" in result.error.message
    assert requests == [(
        "https://93.184.216.34/redirect",
        "public.example",
        "public.example",
    )]


@pytest.mark.asyncio
async def test_web_fetch_does_not_follow_dns_rebinding_to_private_address(
    monkeypatch
):
    import ipaddress
    import socket
    import httpx

    private_requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            private_requests.append(self.path)
            body = b"private service response"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()

    real_getaddrinfo = socket.getaddrinfo
    resolved_hosts = []

    def resolve_to_private_after_policy_check(host, port, *args, **kwargs):
        hostname = host.decode() if isinstance(host, bytes) else host
        resolved_hosts.append(hostname)
        if hostname == "rebind.example":
            return real_getaddrinfo("127.0.0.1", port, *args, **kwargs)
        return real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(
        "XBotv2.browser.impl.network._resolve_addresses",
        lambda _hostname: {ipaddress.ip_address("93.184.216.34")},
    )
    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        "XBotv2.browser.impl.network.httpx.AsyncClient",
        lambda **kwargs: real_async_client(trust_env=False, **kwargs),
    )
    monkeypatch.setattr(
        "XBotv2.browser.impl.network.socket.getaddrinfo",
        resolve_to_private_after_policy_check,
    )
    access = WebAccess(NetworkPolicy(timeout=0.5))
    try:
        result = await access.fetch(
            f"http://rebind.example:{server.server_port}/private"
        )
    finally:
        await access.close()
        server.shutdown()
        thread.join()
        server.server_close()

    assert isinstance(result, ToolFailed)
    assert private_requests == []
    assert "rebind.example" not in resolved_hosts


@pytest.mark.asyncio
async def test_chromium_proxy_rejects_dns_rebinding_at_connection_boundary(
    tmp_path, artifact_store, monkeypatch
):
    import ipaddress
    from threading import Lock

    private_requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            private_requests.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, _format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    resolution_count = 0
    resolution_lock = Lock()

    def rebind_after_browser_preflight(_hostname):
        nonlocal resolution_count
        with resolution_lock:
            resolution_count += 1
            count = resolution_count
        address = "93.184.216.34" if count < 3 else "127.0.0.1"
        return {ipaddress.ip_address(address)}

    monkeypatch.setattr(
        "XBotv2.browser.impl.network._resolve_addresses",
        rebind_after_browser_preflight,
    )
    browser = BrowserSession(
        network_policy=NetworkPolicy(timeout=5),
        session_policy=BrowserSessionPolicy(timeout=5),
        artifacts=artifact_store,
        sandbox=FakeBrowserSandbox(tmp_path),
    )
    try:
        result = await browser.open(
            f"http://rebind.example:{server.server_port}/private"
        )
        assert isinstance(result, ToolFailed), _text(result)
        assert resolution_count >= 3
        assert private_requests == []
    finally:
        await browser.shutdown()
        server.shutdown()
        thread.join()
        server.server_close()


def test_browser_requires_sandbox_policy_at_construction(artifact_store):
    """A browser cannot be constructed without its required policy owner."""
    with pytest.raises(TypeError, match="sandbox"):
        BrowserSession(
            network_policy=NetworkPolicy(),
            session_policy=BrowserSessionPolicy(timeout=5),
            artifacts=artifact_store,
        )


@pytest.mark.asyncio
async def test_browser_proxy_rejects_a_connection_to_its_own_listener():
    proxy = BrowserProxy(NetworkPolicy(private_access=True), network_enabled=True)
    server = await proxy.start()
    proxy_port = int(server.rsplit(":", 1)[1])
    try:
        with pytest.raises(ValueError, match="cannot connect to itself"):
            await proxy._connect(f"http://127.0.0.1:{proxy_port}/loop")
    finally:
        await proxy.close()
