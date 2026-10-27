"""Loopback OpenAI-compatible provider used by the stress infrastructure."""

from __future__ import annotations

import asyncio
import json
import socket
from collections import deque
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse


class LoopbackServer:
    """Own a uvicorn listener, including cleanup when startup fails."""

    def __init__(self, app: Any) -> None:
        self.server = uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", port=0, log_level="warning", lifespan="off",
        ))
        self.task: asyncio.Task[None] | None = None
        self.socket: socket.socket | None = None
        self.base_url = ""

    async def __aenter__(self) -> "LoopbackServer":
        try:
            self.socket = socket.socket()
            self.socket.bind(("127.0.0.1", 0))
            self.socket.setblocking(False)
            self.task = asyncio.create_task(self.server.serve(sockets=[self.socket]))
            async with asyncio.timeout(10):
                while not self.server.started:
                    if self.task.done():
                        await self.task
                        raise RuntimeError("loopback server exited before startup")
                    await asyncio.sleep(0.01)
            port = self.socket.getsockname()[1]
            self.base_url = f"http://127.0.0.1:{port}"
            return self
        except BaseException:
            await self.__aexit__()
            raise

    async def __aexit__(self, *exc_info: object) -> None:
        self.server.should_exit = True
        try:
            if self.task is not None:
                await self.task
        finally:
            if self.socket is not None:
                self.socket.close()


class NetworkMockLLM:
    """Generate deterministic SSE on demand, through a real provider socket.

    Faults affect only the first request. Recent request bodies are bounded so
    the probe can inspect the wire contract without retaining a whole run.
    """

    def __init__(
        self,
        *,
        mock_tools: bool = False,
        fault: str = "",
        chunk_delay_ms: float = 50,
        jitter_ms: float = 0,
        timeout_delay_seconds: float = 0.2,
    ) -> None:
        if fault not in {"", "disconnect", "http_error", "invalid_json", "invalid_utf8", "timeout"}:
            raise ValueError(f"unknown network provider fault: {fault}")
        if chunk_delay_ms < 0 or not 0 <= jitter_ms <= chunk_delay_ms:
            raise ValueError("delay and jitter must satisfy 0 <= jitter <= delay")
        self.mock_tools = mock_tools
        self.fault = fault
        self.chunk_delay_ms = chunk_delay_ms
        self.jitter_ms = jitter_ms
        self.timeout_delay_seconds = timeout_delay_seconds
        self.requests: deque[dict[str, Any]] = deque(maxlen=8)
        self.request_count = 0
        self.app = FastAPI()
        self.app.post("/v1/chat/completions")(self.complete)
        self.listener = LoopbackServer(self.app)
        self.base_url = ""

    async def __aenter__(self) -> "NetworkMockLLM":
        await self.listener.__aenter__()
        self.base_url = f"{self.listener.base_url}/v1"
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.listener.__aexit__(*exc_info)

    async def complete(self, request: Request):
        payload = await request.json()
        if not payload.get("model") or payload.get("stream") is not True or not payload.get("messages"):
            return JSONResponse({"error": {"message": "expected model, messages and stream=true"}}, status_code=400)
        self.requests.append(payload)
        self.request_count += 1
        request_id = self.request_count
        fault = self.fault if request_id == 1 else ""
        if fault == "http_error":
            return JSONResponse({"error": {"message": "injected provider HTTP error"}}, status_code=503)
        if fault == "timeout":
            await asyncio.sleep(self.timeout_delay_seconds)
        return StreamingResponse(self.stream(payload, request_id, fault), media_type="text/event-stream")

    async def stream(self, payload: dict[str, Any], request_id: int, fault: str):
        frame_index = 0

        async def frame(delta: dict[str, Any], finish_reason: str | None = None) -> bytes:
            nonlocal frame_index
            # Alternating jitter is reproducible and never changes the payload.
            delay = self.chunk_delay_ms + (self.jitter_ms if frame_index % 2 else -self.jitter_ms)
            frame_index += 1
            await asyncio.sleep(delay / 1000)
            return self.encode(payload, request_id, [{
                "index": 0, "delta": delta, "finish_reason": finish_reason,
            }])

        yield await frame({"role": "assistant", "reasoning_content": "stress "})
        if fault == "disconnect":
            # Raising after headers leaves an incomplete HTTP chunked body.
            raise ConnectionError("injected provider stream failure after partial output")
        if fault == "invalid_json":
            yield b"data: {invalid json}\n\n"
            return
        if fault == "invalid_utf8":
            yield b"data: \xff\xfe\n\n"
            return
        yield await frame({"reasoning_content": "reasoning"})
        if self.mock_tools and payload["messages"][-1]["role"] != "tool":
            arguments = json.dumps({"command": "printf stress-tool"})
            yield await frame({"tool_calls": [{
                "index": 0, "id": f"stress-shell-{request_id}", "type": "function",
                "function": {"name": "shell", "arguments": arguments[:7]},
            }]})
            yield await frame({"tool_calls": [{
                "index": 0, "function": {"arguments": arguments[7:19]},
            }]})
            yield await frame({"tool_calls": [{
                "index": 0, "function": {"arguments": arguments[19:]},
            }]})
            yield await frame({}, "tool_calls")
        else:
            yield await frame({"content": "stress "})
            yield await frame({"content": "response"})
            yield await frame({}, "stop")
        yield self.encode(payload, request_id, [], usage={
            "prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16,
        })
        yield b"data: [DONE]\n\n"

    @staticmethod
    def encode(payload: dict[str, Any], request_id: int, choices: list, **extra: Any) -> bytes:
        chunk = {
            "id": f"stress-{request_id}", "object": "chat.completion.chunk",
            "created": 1, "model": payload["model"], "choices": choices, **extra,
        }
        return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")
