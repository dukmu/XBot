"""Typed XCore operation routing contracts."""

import pytest
from xcore import Context

from XBotv2.core.errors import OperationError
from XBotv2.core.operations import Operation, dispatch_operation


class Request:
    pass


class Response:
    pass


OPERATION = Operation("test/operation", Request, Response)


@pytest.mark.asyncio
async def test_dispatch_operation_validates_request_and_response() -> None:
    ctx = Context()
    await ctx.start()
    ctx.on(OPERATION.name, lambda _request: Response())

    assert isinstance(
        await dispatch_operation(ctx, OPERATION, Request()),
        Response,
    )
    with pytest.raises(TypeError, match="requires Request"):
        await dispatch_operation(ctx, OPERATION, object())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_dispatch_operation_fails_when_capability_is_absent() -> None:
    ctx = Context()
    await ctx.start()

    with pytest.raises(OperationError) as raised:
        await dispatch_operation(ctx, OPERATION, Request())
    assert raised.value.code == "capability_unavailable"


@pytest.mark.asyncio
async def test_dispatch_operation_rejects_wrong_handler_result() -> None:
    ctx = Context()
    await ctx.start()
    ctx.on(OPERATION.name, lambda _request: object())

    with pytest.raises(TypeError, match="expected Response"):
        await dispatch_operation(ctx, OPERATION, Request())
