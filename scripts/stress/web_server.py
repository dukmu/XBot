"""Disposable Vite server used by the real-browser stress probe."""

from __future__ import annotations

import asyncio
import os
import socket
from pathlib import Path

import httpx


class WebServerHarness:
    """Start the repository WebUI and proxy it to an already running API."""

    def __init__(self, api_url: str, timeout: float) -> None:
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout
        self.project_root = Path(__file__).resolve().parents[2] / "XBotv2" / "web"
        self.process: asyncio.subprocess.Process | None = None
        self.base_url = ""

    async def __aenter__(self) -> "WebServerHarness":
        vite = self.project_root / "node_modules" / ".bin" / "vite"
        if not vite.is_file():
            raise RuntimeError(
                f"WebUI dependencies are missing at {vite}; run npm install in {self.project_root}"
            )
        port = self._free_port()
        self.base_url = f"http://127.0.0.1:{port}"
        environment = os.environ.copy()
        environment["XBOT_API_URL"] = self.api_url
        environment["CI"] = "1"
        self.process = await asyncio.create_subprocess_exec(
            "npm",
            "run",
            "dev",
            "--",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            cwd=self.project_root,
            env=environment,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await self._wait_until_ready()
        except BaseException:
            await self.__aexit__(None, None, None)
            raise
        return self

    @staticmethod
    def _free_port() -> int:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return int(probe.getsockname()[1])

    async def _wait_until_ready(self) -> None:
        deadline = asyncio.get_running_loop().time() + self.timeout
        async with httpx.AsyncClient(timeout=min(self.timeout, 1.0)) as client:
            while True:
                if self.process is not None and self.process.returncode is not None:
                    raise RuntimeError(
                        f"WebUI process exited before startup (code {self.process.returncode})"
                    )
                try:
                    response = await client.get(self.base_url)
                    if response.is_success:
                        return
                except httpx.HTTPError:
                    pass
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError(f"WebUI did not start at {self.base_url}")
                await asyncio.sleep(0.05)

    async def __aexit__(self, *exc_info: object) -> None:
        process = self.process
        self.process = None
        if process is None or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=min(self.timeout, 5.0))
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()


__all__ = ["WebServerHarness"]
