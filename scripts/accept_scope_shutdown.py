#!/usr/bin/env python3
"""Verify CLI SIGTERM drains an active SSE connection and releases ownership."""

from __future__ import annotations
import argparse
import asyncio
import json
import os
from pathlib import Path
import signal
import sys
import time
import httpx
from XBotv2.client import XBotClient
from XBotv2.main import open_runtime
from XBotv2.core.api.paths import RuntimePaths
from XBotv2.persistence.api.contracts import PERSISTENCE
from scripts.stress.provider import NetworkMockLLM
from scripts.stress.server import _write_config


async def run(root: Path, xbot: Path):
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        raise ValueError(f"acceptance root must be empty: {root}")
    (root / "workspace").mkdir()
    async with NetworkMockLLM(mock_tools=False) as provider:
        _write_config(root / "data", provider.base_url)
        socket = root / "server.sock"
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        with (root / "server.log").open("wb") as log:
            process = await asyncio.create_subprocess_exec(
                str(xbot),
                "serve",
                "--data-dir",
                str(root / "data"),
                "--workspace",
                str(root / "workspace"),
                "--provider",
                "default",
                "--uds",
                str(socket),
                cwd=root,
                env=environment,
                stdout=log,
                stderr=log,
            )
            try:
                async with XBotClient(
                    "http://localhost", uds_path=str(socket)
                ) as client:
                    async with asyncio.timeout(20):
                        while True:
                            if process.returncode is not None:
                                raise RuntimeError(
                                    f"server exited; inspect {root / 'server.log'}"
                                )
                            try:
                                await client.health()
                                break
                            except httpx.TransportError:
                                await asyncio.sleep(0.05)
                    await client.open_session(session_id="active-sse", thread_id="main")
                    transport = httpx.AsyncHTTPTransport(uds=str(socket))
                    async with httpx.AsyncClient(
                        transport=transport, base_url="http://localhost", timeout=20
                    ) as raw:
                        async with raw.stream(
                            "GET", "/sessions/active-sse/threads/main/events"
                        ) as stream:
                            assert stream.status_code == 200
                            reading = asyncio.create_task(stream.aread())
                            await asyncio.sleep(0.05)
                            assert not reading.done(), "SSE ended before SIGTERM"
                            started = time.monotonic()
                            process.send_signal(signal.SIGTERM)
                            await asyncio.wait_for(process.wait(), 10)
                            elapsed = time.monotonic() - started
                            body = await asyncio.wait_for(reading, 2)
                if process.returncode not in (0, -signal.SIGTERM):
                    raise RuntimeError(
                        f"server exited {process.returncode}; inspect {root / 'server.log'}"
                    )
                paths = RuntimePaths.from_data_dir(root / "data")
                probe = await open_runtime(paths=paths)
                try:
                    claim = probe.require(PERSISTENCE).acquire("active-sse")
                    claim.release()
                finally:
                    await probe.close()
                report = {
                    "accepted": True,
                    "exit_code": process.returncode,
                    "shutdown_seconds": elapsed,
                    "events_response": 200,
                    "owner_lock_reacquired": True,
                    "stream_live_before_signal": True,
                    "sse_tail_bytes": len(body),
                    "root": str(root),
                    "xbot": str(xbot),
                }
                (root / "report.json").write_text(
                    json.dumps(report, indent=2) + "\n", encoding="utf-8"
                )
                return report
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument(
        "--xbot", type=Path, default=Path(sys.executable).with_name("xbot")
    )
    args = parser.parse_args()
    print(
        json.dumps(
            asyncio.run(run(args.root, args.xbot.expanduser().absolute())), indent=2
        )
    )


if __name__ == "__main__":
    main()
