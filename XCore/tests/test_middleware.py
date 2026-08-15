"""Middleware chain tests: order, short-circuit, filters, errors."""

from __future__ import annotations

import pytest

from xcore import Context


class Session:
    def __init__(self, platform: str) -> None:
        self.platform = platform


async def test_middleware_runs_in_registration_order():
    ctx = Context()
    seen: list[str] = []

    async def m1(session, next):
        seen.append("m1")
        return await next()

    async def m2(session, next):
        seen.append("m2")
        return await next()

    ctx.middleware(m1)
    ctx.middleware(m2)
    result = await ctx.run_middleware(Session("qq"))
    assert result is None
    assert seen == ["m1", "m2"]


async def test_middleware_short_circuits():
    ctx = Context()
    seen: list[str] = []

    async def blocker(session, next):
        seen.append("blocker")
        return "blocked"

    async def later(session, next):
        seen.append("later")
        return await next()

    ctx.middleware(blocker)
    ctx.middleware(later)
    result = await ctx.run_middleware(Session("qq"))
    assert result == "blocked"
    assert seen == ["blocker"]


async def test_middleware_prepend():
    ctx = Context()
    seen: list[str] = []

    async def m1(session, next):
        seen.append("m1")
        return await next()

    async def m2(session, next):
        seen.append("m2")
        return await next()

    ctx.middleware(m1)
    ctx.middleware(m2, prepend=True)
    await ctx.run_middleware(Session("qq"))
    assert seen == ["m2", "m1"]


async def test_middleware_filtered_by_session():
    ctx = Context()
    seen: list[str] = []
    scoped = ctx.select("platform", "qq")

    async def qq_only(session, next):
        seen.append(session.platform)
        return await next()

    scoped.middleware(qq_only)
    await ctx.run_middleware(Session("discord"))
    assert seen == []
    await ctx.run_middleware(Session("qq"))
    assert seen == ["qq"]


async def test_middleware_disposer_removes():
    ctx = Context()
    seen: list[str] = []

    async def m1(session, next):
        seen.append("m1")
        return await next()

    disposer = ctx.middleware(m1)
    assert disposer() is True
    await ctx.run_middleware(Session("qq"))
    assert seen == []


async def test_middleware_error_propagates():
    ctx = Context()

    async def bad(session, next):
        raise ValueError("mw boom")

    ctx.middleware(bad)
    with pytest.raises(ValueError, match="mw boom"):
        await ctx.run_middleware(Session("qq"))


async def test_middleware_removed_with_plugin_fiber():
    ctx = Context()
    seen: list[str] = []

    async def m1(session, next):
        seen.append("m1")
        return await next()

    def plugin(ctx_, config):
        ctx_.middleware(m1)

    await ctx.start()
    handle = ctx.plugin(plugin)
    await handle
    await ctx.run_middleware(Session("qq"))
    assert seen == ["m1"]
    await handle.dispose()
    await ctx.run_middleware(Session("qq"))
    assert seen == ["m1"]  # not called again
