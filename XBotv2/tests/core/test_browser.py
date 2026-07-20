"""Focused behavior tests for the built-in Browser plugin."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from builtin_plugins.browser.network import NetworkOptions, UrlPolicy, WebAccess


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
