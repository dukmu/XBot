"""Observable contracts for application runtime logging."""

from __future__ import annotations

import logging

import httpx
import pytest
import xcore
from fastapi import APIRouter

from XBotv2.application.logging import setup_logging
from XBotv2.core.runtime_logging import (
    RuntimeLog,
    push_log_context,
    reset_log_context,
)
from XBotv2.server.http import create_app


def test_runtime_log_redacts_credentials_but_keeps_usage(caplog) -> None:
    caplog.set_level(logging.INFO, logger="xbotv2.llm")

    RuntimeLog().bind("llm", session_id="s1").info(
        "llm.response",
        api_token="secret-value",
        input_tokens=123,
        usage={"output_tokens": 7, "api_key": "nested-secret"},
    )

    assert "secret-value" not in caplog.text
    assert "nested-secret" not in caplog.text
    assert 'api_token="<redacted>"' in caplog.text
    assert "input_tokens=123" in caplog.text
    assert 'usage={"api_key":"<redacted>","output_tokens":7}' in caplog.text


@pytest.mark.asyncio
async def test_xcore_lifecycle_and_event_records_are_observable(caplog) -> None:
    caplog.set_level(logging.DEBUG, logger="xcore")
    ctx = xcore.Context()

    def component(plugin_ctx, _config=None) -> None:
        plugin_ctx.set("probe", object())
        plugin_ctx.on("probe/event", lambda: None)

    component.name = "probe_plugin"
    ctx.plugin(component)
    await ctx.start()
    await ctx.emit("probe/event")
    await ctx.destroy()

    assert "plugin.state name=probe_plugin" in caplog.text
    assert "service.provided name=probe owner=probe_plugin" in caplog.text
    assert "event.dispatch mode=emit event=probe/event" in caplog.text
    assert "event.handler.finish event=probe/event owner=probe_plugin" in caplog.text
    assert "application.destroyed" in caplog.text


def test_setup_logging_captures_xbot_and_xcore_namespaces(tmp_path) -> None:
    log_file = tmp_path / "runtime.log"
    roots = [logging.getLogger(name) for name in ("xbotv2", "xcore")]
    previous = [
        (list(root.handlers), root.level, root.propagate) for root in roots
    ]
    try:
        setup_logging(log_file=log_file, level="DEBUG")
        logging.getLogger("xbotv2.tools").info("tool.record")
        logging.getLogger("xcore.plugin").info("plugin.record")
        for root in roots:
            for handler in root.handlers:
                handler.flush()

        contents = log_file.read_text(encoding="utf-8")
        assert "xbotv2.tools: tool.record" in contents
        assert "xcore.plugin: plugin.record" in contents
    finally:
        for root, (handlers, level, propagate) in zip(roots, previous, strict=True):
            for handler in root.handlers:
                root.removeHandler(handler)
                handler.close()
            for handler in handlers:
                root.addHandler(handler)
            root.setLevel(level)
            root.propagate = propagate


def test_logging_adds_context_and_honours_category_levels(tmp_path) -> None:
    log_file = tmp_path / "runtime.log"
    setup_logging(
        log_file=log_file,
        level="INFO",
        category_levels={"xcore.events": "WARNING", "xbotv2.tools": "DEBUG"},
    )
    token = push_log_context(request_id="req-7", session_id="session-3")
    try:
        logging.getLogger("xcore.events").info("hidden event")
        logging.getLogger("xbotv2.tools").debug("visible detail")
        RuntimeLog("tools").info("tool.done", session_id="session-3")
    finally:
        reset_log_context(token)
    for root_name in ("xbotv2", "xcore"):
        for handler in logging.getLogger(root_name).handlers:
            handler.flush()

    contents = log_file.read_text(encoding="utf-8")
    assert "hidden event" not in contents
    assert "visible detail request_id=\"req-7\" session_id=\"session-3\"" in contents
    assert contents.count('session_id="session-3"') == 2
    assert 'tool.done session_id="session-3" request_id="req-7"' in contents


def test_logging_keeps_traceback_but_omits_exception_message(tmp_path) -> None:
    log_file = tmp_path / "runtime.log"
    setup_logging(log_file=log_file, level="ERROR")
    try:
        raise RuntimeError("private-provider-response")
    except RuntimeError:
        logging.getLogger("xbotv2.llm").exception("provider failed")
    for handler in logging.getLogger("xbotv2").handlers:
        handler.flush()

    contents = log_file.read_text(encoding="utf-8")
    assert "provider failed" in contents
    assert "RuntimeError" in contents
    assert "test_logging_keeps_traceback" in contents
    assert "private-provider-response" not in contents


@pytest.mark.asyncio
async def test_api_logs_request_response_and_correlation_id() -> None:
    records: list[tuple[str, dict[str, object]]] = []

    class RecordingLog:
        def bind(self, _category: str):
            return self

        def info(self, event: str, **fields: object) -> None:
            records.append((event, fields))

        def log(self, _level: int, event: str, **fields: object) -> None:
            records.append((event, fields))

        def error(self, event: str, **fields: object) -> None:
            records.append((event, fields))

        def exception(self, event: str, **fields: object) -> None:
            records.append((event, fields))

    app = create_app(runtime_log=RecordingLog())
    router = APIRouter()

    @router.get("/probe")
    async def probe() -> dict[str, bool]:
        return {"ok": True}

    @router.get("/failure")
    async def failure() -> None:
        raise RuntimeError("private failure detail")

    app.include_router(router)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/probe",
            headers={"x-request-id": "req-1"},
        )

    assert response.status_code == 200
    assert response.headers["x-request-id"] == "req-1"
    assert [event for event, _fields in records] == [
        "api.request",
        "api.response",
    ]
    assert records[-1][1]["status"] == 200
    assert records[-1][1]["http_request_id"] == "req-1"
    assert records[-1][1]["incomplete"] is False

    records.clear()
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        failed = await client.get(
            "/failure",
            headers={"x-request-id": "req-2"},
        )
    assert failed.status_code == 500
    assert records[-1][0] == "api.response"
    assert records[-1][1]["http_request_id"] == "req-2"
    assert records[-1][1]["error_type"] == "RuntimeError"
    assert "private failure detail" not in str(records)
