"""Compiled Web server and API proxy tests."""

from pathlib import Path

import httpx
import pytest

from xbotv2 import web_server


def _static_root(tmp_path: Path) -> Path:
    root = tmp_path / "web"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<main>XBot Web</main>", encoding="utf-8")
    (root / "assets" / "app.js").write_text("window.XBOT = true", encoding="utf-8")
    return root


@pytest.mark.asyncio
async def test_web_app_serves_assets_and_spa_routes(tmp_path):
    app = web_server.create_web_app(
        _static_root(tmp_path),
        api_url="http://localhost",
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        assert (await client.get("/")).text == "<main>XBot Web</main>"
        assert (
            await client.get("/sessions/current")
        ).text == "<main>XBot Web</main>"
        assert (await client.get("/assets/app.js")).text == "window.XBOT = true"


@pytest.mark.asyncio
async def test_web_app_proxies_api_without_prefix(tmp_path, monkeypatch):
    observed = {}
    asgi_client = httpx.AsyncClient

    class FakeClient:
        def __init__(self, **kwargs):
            observed["client"] = kwargs

        def build_request(self, method, url, **kwargs):
            observed.update(method=method, url=url, request=kwargs)
            return httpx.Request(method, f"http://localhost{url}")

        async def send(self, request, *, stream):
            observed["stream"] = stream
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                stream=httpx.ByteStream(b'{"status":"ok"}'),
                request=request,
            )

        async def aclose(self):
            observed["closed"] = True

    monkeypatch.setattr(web_server.httpx, "AsyncClient", FakeClient)
    app = web_server.create_web_app(
        _static_root(tmp_path),
        api_url="http://localhost",
        uds_path="/tmp/xbot.sock",
    )

    async with asgi_client(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/api/health?verbose=true")

    assert response.json() == {"status": "ok"}
    assert observed["method"] == "GET"
    assert observed["url"] == "/health"
    assert str(observed["request"]["params"]) == "verbose=true"
    assert observed["stream"] is True
    assert observed["closed"] is True
