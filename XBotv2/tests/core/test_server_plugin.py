"""Server host and dumb web-carrier plugin tests."""

from pathlib import Path

import pytest
import pytest_asyncio
import httpx
import yaml
from fastapi import APIRouter, FastAPI

from XBotv2.core.paths import RuntimePaths
from XBotv2.server.contracts import REGISTER_ROUTE, RouteContribution
from XBotv2.server.plugin import WebServer, route_keys


def test_web_server_register_is_an_effect() -> None:
    """A mounted router is removed exactly when its disposer runs (DSH-style)."""

    app = FastAPI()
    carrier = WebServer(app)

    router = APIRouter()

    @router.get("/hmr/marker")
    async def marker() -> dict[str, bool]:
        return {"ok": True}

    def paths() -> set[str]:
        return {path for _method, path in route_keys(app)}

    assert "/hmr/marker" not in paths()
    dispose = carrier.register(router)
    assert "/hmr/marker" in paths()

    dispose()
    assert "/hmr/marker" not in paths()


@pytest.mark.asyncio
async def test_mounted_router_is_dispatched_until_its_disposer_runs() -> None:
    """Registration is an effect on real dispatch, not only on route bookkeeping."""

    app = FastAPI()
    carrier = WebServer(app)

    router = APIRouter()

    @router.get("/effect/probe")
    async def probe() -> dict[str, bool]:
        return {"ok": True}

    dispose = carrier.register(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        assert (await client.get("/effect/probe")).json() == {"ok": True}
        dispose()
        assert (await client.get("/effect/probe")).status_code == 404


def test_web_server_duplicate_path_is_rejected() -> None:
    """A second router owning the same path is a composition misconfiguration."""

    app = FastAPI()
    carrier = WebServer(app)

    first = APIRouter()

    @first.get("/dup")
    async def one() -> dict[str, str]:
        return {"who": "first"}

    carrier.register(first)

    second = APIRouter()

    @second.get("/dup")
    async def two() -> dict[str, str]:
        return {"who": "second"}

    try:
        carrier.register(second)
    except Exception:
        return
    raise AssertionError("duplicate path registration must raise")


@pytest_asyncio.fixture
async def booted_server(tmp_path: Path):
    from XBotv2.application.server import start_server_application

    data_dir = tmp_path / "data"
    (data_dir / "config").mkdir(parents=True)
    (data_dir / "config" / "plugins.yaml").write_text(
        yaml.safe_dump([
            {
                "id": "llm",
                "name": "llm",
                "config": {
                    "default": "default",
                    "providers": {
                        "default": {
                            "protocol": "openai",
                            "base_url": "http://test",
                            "api_key": "test",
                            "default_model": "test",
                            "models": [
                                {
                                    "model": "test",
                                    "max_context_tokens": 4096,
                                },
                            ],
                        },
                    },
                },
            },
        ], sort_keys=False),
        encoding="utf-8",
    )
    (data_dir / "config" / "config.yaml").write_text(
        "provider: default\ntools: []\nplugins: {}\nhooks: []\n"
        "sandbox:\n  enabled: false\n  resources: []\n",
        encoding="utf-8",
    )
    server = await start_server_application(
        provider_name="default",
        paths=RuntimePaths.from_data_dir(data_dir),
        workspace_root=str(tmp_path),
        no_plugins=True,
    )
    try:
        yield server
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_server_composition_root_owns_sessions(booted_server) -> None:
    """Sessions is an XCore service, not hidden FastAPI carrier state."""

    app = booted_server.server
    assert booted_server.sessions is not None
    assert not hasattr(app.state, "manager")
    assert not hasattr(booted_server, "web_server")


@pytest.mark.asyncio
async def test_router_contribution_is_routed_through_xcore(booted_server) -> None:
    router = APIRouter()

    @router.get("/event-owned")
    async def event_owned() -> dict[str, bool]:
        return {"ok": True}

    dispose = await booted_server.bail(
        REGISTER_ROUTE,
        RouteContribution(owner="test", router=router),
    )
    assert callable(dispose)
    assert "/event-owned" in {path for _method, path in route_keys(booted_server.server)}
    dispose()
    assert "/event-owned" not in {path for _method, path in route_keys(booted_server.server)}
