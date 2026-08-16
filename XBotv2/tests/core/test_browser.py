"""Focused behavior tests for the built-in Browser plugin."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

from browser.browser import BrowserSession
from browser.network import NetworkOptions, UrlPolicy, WebAccess
from api import ToolResult


class FakeBrowserSandbox:
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
async def test_browser_open_http_uses_unified_network_guard(tmp_path):
    browser = BrowserSession(
        policy=UrlPolicy(),
        artifacts_dir=tmp_path,
        headless=True,
        timeout_seconds=5,
    )

    result = await browser.open(
        "https://example.com/page",
        sandbox=NoNetworkSandbox(tmp_path),
    )

    assert result.status == "error"
    assert result.error is not None
    assert result.error.code == "network_disabled"


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
    access = WebAccess(NetworkOptions())
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

    assert result.status == "success"
    assert result.data["results"] == [{
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
    access = WebAccess(NetworkOptions())
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

    assert result.status == "error"
    assert result.error is not None
    assert result.error.code == "search_failed"


@pytest.mark.asyncio
async def test_browser_file_url_resolves_inside_sandbox(tmp_path):
    page = tmp_path / "page.html"
    page.write_text("<h1>local</h1>", encoding="utf-8")
    sandbox = FakeBrowserSandbox(tmp_path)
    browser = BrowserSession(
        policy=UrlPolicy(),
        artifacts_dir=tmp_path,
        headless=True,
        timeout_seconds=5,
    )

    target = await browser._target_url(page.as_uri(), sandbox)

    assert target == "file://" + str(page)


@pytest.mark.asyncio
async def test_browser_file_url_rejects_path_outside_sandbox(tmp_path):
    outside = tmp_path.parent / "outside.html"
    sandbox = FakeBrowserSandbox(tmp_path)
    browser = BrowserSession(
        policy=UrlPolicy(),
        artifacts_dir=tmp_path,
        headless=True,
        timeout_seconds=5,
    )

    with pytest.raises(ValueError, match="outside the sandbox"):
        await browser._target_url(outside.as_uri(), sandbox)


@pytest.mark.asyncio
async def test_browser_open_accepts_file_url_with_sandbox(tmp_path):
    class FakeBrowser(BrowserSession):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.opened = ""

        async def _ensure_page(self, sandbox=None):
            self._sandbox = sandbox
            return self

        async def goto(self, url, **_kwargs):
            self.opened = url

        async def snapshot(self):
            return ToolResult.success("snapshot")

    page = tmp_path / "page.html"
    page.write_text("<h1>local</h1>", encoding="utf-8")
    browser = FakeBrowser(
        policy=UrlPolicy(),
        artifacts_dir=tmp_path,
        headless=True,
        timeout_seconds=5,
    )

    result = await browser.open(
        page.as_uri(),
        sandbox=FakeBrowserSandbox(tmp_path),
    )

    assert result.status == "success"
    assert browser.opened == "file://" + str(page)


@pytest.mark.asyncio
async def test_url_policy_blocks_private_destinations():
    with pytest.raises(ValueError, match="Private, local"):
        await UrlPolicy().check("http://127.0.0.1/private")

    assert await UrlPolicy().check("https://93.184.216.34/page") == (
        "https://93.184.216.34/page"
    )


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
    access = WebAccess(NetworkOptions(allow_private=True))
    try:
        result = await access.fetch(
            f"http://127.0.0.1:{server.server_port}/article"
        )
    finally:
        await access.close()
        server.shutdown()
        thread.join()
        server.server_close()

    assert result.status == "success"
    assert "Release notes" in result.content
    assert result.data["content_type"] == "text/html"
    assert result.data["url"].endswith("/article")
    assert result.data["untrusted"] is True


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
    access = WebAccess(NetworkOptions(max_response_bytes=64, allow_private=True))
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        redirected = await access.fetch(f"{base_url}/redirect")
        oversized = await access.fetch(f"{base_url}/large")
    finally:
        await access.close()
        server.shutdown()
        thread.join()
        server.server_close()

    assert redirected.status == "success"
    assert redirected.data["url"].endswith("/final")
    assert "redirect complete" in redirected.content
    assert oversized.status == "error"
    assert oversized.error is not None
    assert oversized.error.code == "fetch_failed"
    assert "size limit" in oversized.error.message
