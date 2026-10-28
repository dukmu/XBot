#!/usr/bin/env python3
"""Verify CLI startup, provider streaming, and recovery across two processes."""

from __future__ import annotations

import argparse
import asyncio
import base64
from contextlib import aclosing, asynccontextmanager
import json
import os
from pathlib import Path
import signal
import sys

import httpx

from XBotv2.client import XBotClient
from scripts.stress.provider import NetworkMockLLM
from scripts.stress.server import _write_config

SESSION_ID = "runtime-acceptance"
THREAD_ID = "main"
ATTACHMENT = b"runtime acceptance attachment\n"


@asynccontextmanager
async def _server(root: Path, xbot: Path, phase: str):
    """Use the selected installed console entry point outside the checkout."""
    socket_path = root / "server.sock"
    socket_path.unlink(missing_ok=True)
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    with (root / f"{phase}.log").open("wb") as log:
        process = await asyncio.create_subprocess_exec(
            str(xbot), "serve",
            "--data-dir", str(root / "data"),
            "--workspace", str(root / "workspace"),
            "--provider", "default", "--uds", str(socket_path),
            cwd=root, env=environment, stdout=log, stderr=log,
        )
        try:
            async with XBotClient("http://localhost", uds_path=str(socket_path)) as client:
                async with asyncio.timeout(20):
                    while True:
                        if process.returncode is not None:
                            raise RuntimeError(f"server exited; inspect {root / f'{phase}.log'}")
                        try:
                            await client.health()
                            break
                        except httpx.TransportError:
                            await asyncio.sleep(0.05)
                yield client, process.pid
        finally:
            stopping = process.returncode is None
            if stopping:
                process.terminate()
            try:
                async with asyncio.timeout(10):
                    await process.wait()
            except TimeoutError:
                process.kill()
                await process.wait()
                raise RuntimeError(f"server failed to stop; inspect {root / f'{phase}.log'}")
            finally:
                socket_path.unlink(missing_ok=True)
            # Uvicorn re-raises the received signal after its server exits.
            expected = {0, -signal.SIGTERM} if stopping else {0}
            if process.returncode not in expected:
                raise RuntimeError(f"server exited {process.returncode}; inspect {root / f'{phase}.log'}")


async def _turn(client: XBotClient, *, cursor: int, number: int):
    await client.send_message(
        SESSION_ID, THREAD_ID, f"acceptance input {number}",
        request_id=f"acceptance-{number}",
        attachments=[{
            "name": "acceptance.txt", "media_type": "text/plain",
            "data": base64.b64encode(ATTACHMENT).decode("ascii"),
        }] if number == 1 else None,
    )
    events = []
    async with asyncio.timeout(20), aclosing(
        client.stream_events(SESSION_ID, THREAD_ID, after=cursor)
    ) as stream:
        async for event in stream:
            events.append(event)
            if event.kind == "error":
                raise RuntimeError(f"turn failed: {event.payload}")
            if event.kind == "turn_ended":
                if event.payload["outcome"]["kind"] != "finished":
                    raise RuntimeError(f"turn did not finish: {event.payload}")
                break
        else:
            raise RuntimeError("event stream ended without turn_ended")
    for kind in ("assistant_text_delta", "assistant_reasoning_delta", "tool_completed", "usage"):
        if not any(event.kind == kind for event in events):
            raise RuntimeError(f"missing streamed {kind}")
    history = await client.list_messages(SESSION_ID, THREAD_ID)
    if not history.items or history.items[-1].kind != "assistant" or history.items[-1].content != "stress response":
        raise RuntimeError("canonical history did not retain the provider response")
    return history.items, [event.kind for event in events]


async def _run(root: Path, xbot: Path) -> dict:
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        raise ValueError(f"acceptance root must be empty: {root}")
    (root / "workspace").mkdir()
    async with NetworkMockLLM(mock_tools=True, chunk_delay_ms=5) as provider:
        _write_config(root / "data", provider.base_url)
        async with _server(root, xbot, "first") as (client, first_pid):
            if (await client.list_sessions()).sessions:
                raise RuntimeError("idle startup listed a session")
            sessions = root / "data" / "sessions"
            if sessions.exists() and any(sessions.iterdir()):
                raise RuntimeError("idle startup persisted a session")
            opened = (await client.open_session(
                session_id=SESSION_ID, thread_id=THREAD_ID,
                workspace_root=str(root / "workspace"),
            )).data
            first_history, first_events = await _turn(client, cursor=opened.event_cursor, number=1)
            attachment = next(item for item in first_history if item.kind == "human_input").artifacts[0]
            if await client.read_artifact(SESSION_ID, THREAD_ID, attachment.id) != ATTACHMENT:
                raise RuntimeError("attachment bytes changed")

        async with _server(root, xbot, "resumed") as (client, second_pid):
            restored = (await client.open_session(
                session_id=SESSION_ID, thread_id=THREAD_ID, mode="resume",
            )).data
            if restored.history.items != first_history:
                raise RuntimeError("canonical history changed across process restart")
            if await client.read_artifact(SESSION_ID, THREAD_ID, attachment.id) != ATTACHMENT:
                raise RuntimeError("attachment bytes changed across process restart")
            final_history, second_events = await _turn(client, cursor=restored.event_cursor, number=2)
            inputs = [item.content for item in final_history if item.kind == "human_input"]
            if inputs != ["acceptance input 1", "acceptance input 2"]:
                raise RuntimeError(f"recovered input missing or duplicated: {inputs}")
        if first_pid == second_pid or provider.request_count != 4:
            raise RuntimeError("expected two server processes and four network model requests")
        report = {
            "accepted": True,
            "xbot": str(xbot),
            "process_ids": [first_pid, second_pid],
            "provider_requests": provider.request_count,
            "messages_after_restart": len(first_history),
            "messages_after_followup": len(final_history),
            "attachment_bytes": len(ATTACHMENT),
            "first_events": first_events,
            "followup_events": second_events,
            "scope": "CLI + real sockets + OpenAI adapter + process restart; no external provider",
        }
    (root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="new empty directory for data, logs and report")
    parser.add_argument("--xbot", type=Path, default=Path(sys.executable).with_name("xbot"), help="console entry point; use an independent wheel environment to verify installation")
    args = parser.parse_args()
    report = asyncio.run(_run(args.root, args.xbot.expanduser().absolute()))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
