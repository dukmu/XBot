#!/usr/bin/env python3
"""Exercise assembled logging and persistence and retain inspectable evidence."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

import httpx
import yaml

from XBotv2.application.logging import setup_logging
from XBotv2.application.server import start_server_application
from XBotv2.core.paths import RuntimePaths
from XBotv2.core.runtime_logging import RuntimeLog
from XBotv2.llm.mock import MockLLM
from XBotv2.server.http import set_llm_override


SESSION_ID = "runtime-acceptance"
THREAD_ID = "main"
USER_MARKER = "XBOT_ACCEPT_USER_9d47b2"
ATTACHMENT_MARKER = b"XBOT_ACCEPT_ATTACHMENT_40c1a8"
API_KEY_MARKER = "XBOT_ACCEPT_API_KEY_739af0"
PENDING_MARKER = "XBOT_ACCEPT_PENDING_51ab8e"
ERROR_MARKER = "XBOT_ACCEPT_ERROR_INPUT_80bc6f"


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        type=Path,
        required=True,
        help="new empty directory that will retain logs, data, and report.json",
    )
    parser.add_argument(
        "--phase",
        choices=("first", "second", "crash", "recover", "logging_edges", "corruption"),
        help=argparse.SUPPRESS,
    )
    return parser.parse_args()


def _prepare(root: Path) -> tuple[RuntimePaths, Path]:
    root = root.resolve()
    if root.exists() and any(root.iterdir()):
        raise RuntimeError(f"acceptance root must be empty: {root}")
    data_dir = root / "data"
    workspace = root / "workspace"
    config_dir = data_dir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    (config_dir / "plugins.yaml").write_text(
        yaml.safe_dump(
            [
                {
                    "id": "llm",
                    "name": "llm",
                    "config": {
                        "default": "default",
                        "providers": {
                            "default": {
                                "protocol": "openai",
                                "base_url": "http://acceptance.invalid",
                                "api_key": API_KEY_MARKER,
                                "default_model": "acceptance-model",
                                "models": [
                                    {
                                        "model": "acceptance-model",
                                        "max_context_tokens": 4096,
                                        "input_modalities": ["text", "image"],
                                    }
                                ],
                            }
                        },
                    },
                },
                {
                    "id": "config",
                    "name": "config",
                    "config": {
                        "user": {
                            "user_id": "runtime-acceptance",
                            "user_name": "Runtime Acceptance",
                            "platform": "http",
                            "session_type": "interactive",
                        }
                    },
                },
                {
                    "id": "sandbox",
                    "name": "sandbox",
                    "config": {
                        "sandbox": {"enabled": False, "resources": []}
                    },
                },
            ],
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return RuntimePaths.from_data_dir(data_dir), workspace


async def _request(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    **kwargs: Any,
) -> httpx.Response:
    response = await client.request(method, path, **kwargs)
    if response.status_code >= 400:
        raise RuntimeError(
            f"{method} {path} returned {response.status_code}: {response.text}"
        )
    return response


async def _start(
    paths: RuntimePaths,
    workspace: Path,
    responses: list[dict[str, Any]],
) -> Any:
    application = await start_server_application(
        paths=paths,
        provider_name="default",
        workspace_root=str(workspace),
        no_plugins=False,
    )
    set_llm_override(
        application.server,
        MockLLM(responses=responses, input_modalities=["text", "image"]),
    )
    return application


async def _first_process(paths: RuntimePaths, workspace: Path) -> dict[str, Any]:
    application = await _start(
        paths,
        workspace,
        [
            {
                "tool_calls": [
                    {
                        "id": "todo-call",
                        "name": "update_todos",
                        "args": {
                            "todos": [
                                {
                                    "content": "Inspect persisted state",
                                    "status": "in_progress",
                                }
                            ]
                        },
                    }
                ],
                "usage_metadata": {
                    "input_tokens": 11,
                    "output_tokens": 5,
                    "total_tokens": 16,
                },
            },
            {
                "content": "First acceptance response.",
                "usage_metadata": {
                    "input_tokens": 17,
                    "output_tokens": 7,
                    "total_tokens": 24,
                },
            },
            {
                "content": "Second acceptance response.",
                "usage_metadata": {
                    "input_tokens": 13,
                    "output_tokens": 6,
                    "total_tokens": 19,
                },
            },
            {
                "tool_calls": [
                    {
                        "id": "subagent-call",
                        "name": "spawn_subagent",
                        "args": {
                            "agent": "default",
                            "prompt": "Return the runtime acceptance result.",
                            "name": "runtime-acceptance-child",
                        },
                    }
                ]
            },
            {"content": "Subagent acceptance completed."},
            {"content": "Subagent acceptance completed."},
            {
                "tool_calls": [
                    {
                        "id": "goal-complete-call",
                        "name": "update_goal",
                        "args": {
                            "status": "complete",
                            "summary": "Runtime acceptance evidence retained.",
                        },
                    }
                ]
            },
            {"content": "Goal acceptance completed."},
        ],
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application.server),
            base_url="http://acceptance",
        ) as client:
            workspace_response = await _request(
                client,
                "POST",
                "/workspaces",
                json={"path": str(workspace)},
            )
            workspace_data = workspace_response.json()["workspace"]
            await _request(
                client,
                "POST",
                "/sessions",
                json={
                    "session_id": SESSION_ID,
                    "thread_id": THREAD_ID,
                    "workspace_root": str(workspace),
                    "mode": "new",
                },
            )
            first = await _request(
                client,
                "POST",
                f"/sessions/{SESSION_ID}/threads/{THREAD_ID}/messages",
                headers={"x-request-id": "accept-http-first"},
                json={
                    "request_id": "accept-turn-first",
                    "content": USER_MARKER,
                    "attachments": [
                        {
                            "name": "acceptance.txt",
                            "media_type": "text/plain",
                            "data": base64.b64encode(ATTACHMENT_MARKER).decode(),
                        }
                    ],
                },
            )
            if '"type": "end"' not in first.text:
                raise RuntimeError("first message stream has no terminal event")
            second = await _request(
                client,
                "POST",
                f"/sessions/{SESSION_ID}/threads/{THREAD_ID}/messages",
                json={
                    "request_id": "accept-turn-second",
                    "content": "second acceptance input",
                },
            )
            if '"type": "end"' not in second.text:
                raise RuntimeError("second message stream has no terminal event")
            child_turn = await _request(
                client,
                "POST",
                f"/sessions/{SESSION_ID}/threads/{THREAD_ID}/messages",
                json={
                    "request_id": "accept-turn-subagent",
                    "content": "exercise the child lifecycle",
                },
            )
            if '"type": "end"' not in child_turn.text:
                raise RuntimeError("subagent message stream has no terminal event")
            tasks: list[dict[str, Any]] = []
            for _ in range(100):
                tasks = (
                    await _request(
                        client,
                        "GET",
                        f"/sessions/{SESSION_ID}/threads/{THREAD_ID}/tasks",
                    )
                ).json()["tasks"]
                if tasks and all(
                    task["status"] in {"completed", "failed", "stopped"}
                    for task in tasks
                ):
                    break
                await asyncio.sleep(0.01)
            if not tasks or any(task["status"] != "completed" for task in tasks):
                raise RuntimeError(f"subagent lifecycle did not complete: {tasks}")
            goal_created = await _request(
                client,
                "POST",
                f"/sessions/{SESSION_ID}/threads/{THREAD_ID}/commands",
                json={
                    "command": "goal",
                    "raw": "/goal --token-budget 1000 Retain runtime acceptance evidence",
                },
            )
            if goal_created.json()["data"]["status"] != "ok":
                raise RuntimeError(f"goal creation failed: {goal_created.text}")
            goal_message = ""
            for _ in range(100):
                goal = await _request(
                    client,
                    "POST",
                    f"/sessions/{SESSION_ID}/threads/{THREAD_ID}/commands",
                    json={"command": "goal", "raw": "/goal"},
                )
                goal_message = goal.json()["data"]["message"]
                if goal_message.startswith("[complete]"):
                    break
                await asyncio.sleep(0.01)
            else:
                raise RuntimeError(f"goal did not complete: {goal_message}")
            for _ in range(100):
                threads = (
                    await _request(client, "GET", f"/sessions/{SESSION_ID}/threads")
                ).json()["threads"]
                main = next(
                    item for item in threads if item["thread_id"] == THREAD_ID
                )
                if main["turn_status"] == "idle":
                    break
                await asyncio.sleep(0.01)
            else:
                raise RuntimeError("goal continuation did not return to idle")
            await _request(
                client,
                "PATCH",
                f"/sessions/{SESSION_ID}/policy",
                json={"permissions": {"read": "allow"}},
            )
            messages = (
                await _request(
                    client,
                    "GET",
                    f"/sessions/{SESSION_ID}/threads/{THREAD_ID}/messages",
                )
            ).json()["messages"]
            todos = (
                await _request(
                    client,
                    "GET",
                    f"/sessions/{SESSION_ID}/threads/{THREAD_ID}/todos",
                )
            ).json()["items"]
            return {
                "pid": os.getpid(),
                "messages": len(messages),
                "todos": todos,
                "tasks": tasks,
                "goal": goal_message,
                "workspace": workspace_data,
            }
    finally:
        await application.stop()


async def _second_process(paths: RuntimePaths, workspace: Path) -> dict[str, Any]:
    application = await _start(
        paths,
        workspace,
        [
            {"content": "Persisted conversation summary."},
            {
                "content": "Regenerated acceptance response.",
                "usage_metadata": {
                    "input_tokens": 19,
                    "output_tokens": 8,
                    "total_tokens": 27,
                },
            }
        ],
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application.server),
            base_url="http://acceptance",
        ) as client:
            reopened = await _request(
                client,
                "POST",
                "/sessions",
                json={
                    "session_id": SESSION_ID,
                    "thread_id": THREAD_ID,
                    "mode": "resume",
                },
            )
            reopened_data = reopened.json()
            listed_sessions = (
                await _request(client, "GET", "/sessions")
            ).json()["sessions"]
            listed = next(
                item for item in listed_sessions if item["session_id"] == SESSION_ID
            )
            threads = (
                await _request(client, "GET", f"/sessions/{SESSION_ID}/threads")
            ).json()["threads"]
            main_thread = next(
                item for item in threads if item["thread_id"] == THREAD_ID
            )
            workspaces = (
                await _request(client, "GET", "/workspaces")
            ).json()["items"]
            registered_workspace = next(
                item for item in workspaces if item["path"] == str(workspace)
            )
            if registered_workspace["session_ids"] != [SESSION_ID]:
                raise RuntimeError(
                    f"workspace membership did not restore: {registered_workspace}"
                )
            if {
                reopened_data["workspace_root"],
                listed["workspace_root"],
                main_thread["workspace_root"],
            } != {str(workspace)}:
                raise RuntimeError("workspace metadata changed across restart")
            if main_thread["agent"] != "default" or not main_thread["provider"]:
                raise RuntimeError(f"thread metadata did not restore: {main_thread}")
            renamed = (
                await _request(
                    client,
                    "PATCH",
                    f"/sessions/{SESSION_ID}",
                    json={"title": "Runtime acceptance renamed"},
                )
            ).json()
            renamed_thread = (
                await _request(
                    client,
                    "GET",
                    f"/sessions/{SESSION_ID}/threads/{THREAD_ID}",
                )
            ).json()
            if renamed["title"] != "Runtime acceptance renamed" or renamed_thread["title"] != renamed["title"]:
                raise RuntimeError("session rename did not update authoritative thread metadata")
            archived = (
                await _request(
                    client,
                    "PUT",
                    f"/sessions/{SESSION_ID}/archive",
                )
            ).json()["archived_session_ids"]
            if archived != [SESSION_ID]:
                raise RuntimeError(f"session archive did not persist: {archived}")
            todos = (
                await _request(
                    client,
                    "GET",
                    f"/sessions/{SESSION_ID}/threads/{THREAD_ID}/todos",
                )
            ).json()["items"]
            messages = (
                await _request(
                    client,
                    "GET",
                    f"/sessions/{SESSION_ID}/threads/{THREAD_ID}/messages",
                )
            ).json()["messages"]
            artifact = messages[0]["artifacts"][0]
            downloaded = await _request(
                client,
                "GET",
                f"/sessions/{SESSION_ID}/threads/{THREAD_ID}/artifacts/{artifact['id']}",
            )
            if downloaded.content != ATTACHMENT_MARKER:
                raise RuntimeError("artifact bytes changed across restart")
            goal = await _request(
                client,
                "POST",
                f"/sessions/{SESSION_ID}/threads/{THREAD_ID}/commands",
                json={"command": "goal", "raw": "/goal"},
            )
            goal_message = goal.json()["data"]["message"]
            if not goal_message.startswith("[complete]"):
                raise RuntimeError(f"goal did not survive restart: {goal_message}")
            before_compact = len(messages)
            compacted = await _request(
                client,
                "POST",
                f"/sessions/{SESSION_ID}/threads/{THREAD_ID}/commands",
                json={"command": "compact", "raw": "/compact"},
            )
            compact_data = compacted.json()["data"]
            if compact_data["status"] != "ok" or "history" not in compact_data["effects"]:
                raise RuntimeError(f"compaction did not replace history: {compacted.text}")
            after_compact = len(
                (
                    await _request(
                        client,
                        "GET",
                        f"/sessions/{SESSION_ID}/threads/{THREAD_ID}/messages",
                    )
                ).json()["messages"]
            )
            if after_compact >= before_compact:
                raise RuntimeError(
                    f"compaction did not reduce history: {before_compact} -> {after_compact}"
                )
            await _request(
                client,
                "POST",
                f"/sessions/{SESSION_ID}/threads/{THREAD_ID}/history/undo",
                json={"count": 1, "history_limit": 100},
            )
            regenerated = await _request(
                client,
                "POST",
                f"/sessions/{SESSION_ID}/threads/{THREAD_ID}/history/regenerate",
                json={"request_id": "accept-turn-regenerate"},
            )
            if '"type": "end"' not in regenerated.text:
                raise RuntimeError("regenerate stream has no terminal event")
            forked = (
                await _request(
                    client,
                    "POST",
                    f"/sessions/{SESSION_ID}/fork",
                )
            ).json()["session_id"]
            workspace_after_fork = next(
                item
                for item in (
                    await _request(client, "GET", "/workspaces")
                ).json()["items"]
                if item["workspace_id"] == registered_workspace["workspace_id"]
            )
            if workspace_after_fork["session_ids"] != [forked, SESSION_ID]:
                raise RuntimeError(
                    f"new session was not prepended: {workspace_after_fork['session_ids']}"
                )
            ordered_workspace = (
                await _request(
                    client,
                    "POST",
                    f"/workspaces/{registered_workspace['workspace_id']}"
                    f"/sessions/{SESSION_ID}/order",
                    json={"before_session_id": forked},
                )
            ).json()["workspace"]
            if ordered_workspace["session_ids"] != [SESSION_ID, forked]:
                raise RuntimeError(
                    f"session order did not commit: {ordered_workspace['session_ids']}"
                )
            fork_threads = (
                await _request(client, "GET", f"/sessions/{forked}/threads")
            ).json()["threads"]
            fork_main = next(
                item["thread_id"]
                for item in fork_threads
                if item["kind"] == "main"
            )
            await _request(
                client,
                "POST",
                "/sessions",
                json={
                    "session_id": forked,
                    "thread_id": fork_main,
                    "mode": "resume",
                },
            )
            await _request(
                client,
                "POST",
                f"/sessions/{forked}/threads/{fork_main}/history/clear",
            )
            source_messages = (
                await _request(
                    client,
                    "GET",
                    f"/sessions/{SESSION_ID}/threads/{THREAD_ID}/messages",
                )
            ).json()["messages"]
            fork_messages = (
                await _request(
                    client,
                    "GET",
                    f"/sessions/{forked}/threads/{fork_main}/messages",
                )
            ).json()["messages"]
            if not source_messages or fork_messages:
                raise RuntimeError("fork history is not isolated")
            return {
                "pid": os.getpid(),
                "reopened_messages": len(reopened_data["history"]),
                "reopened_usage": reopened_data["usage"],
                "reopened_workspace": reopened_data["workspace_root"],
                "listed_session": {
                    "workspace_root": listed["workspace_root"],
                    "title": listed["title"],
                },
                "thread_metadata": {
                    "agent": main_thread["agent"],
                    "provider": main_thread["provider"],
                    "parent_thread_id": main_thread["parent_thread_id"],
                    "title": main_thread["title"],
                },
                "registered_workspace": registered_workspace,
                "renamed_title": renamed["title"],
                "archived_session_ids": archived,
                "todos": todos,
                "goal": goal_message,
                "compaction": {
                    "messages_before": before_compact,
                    "messages_after": after_compact,
                },
                "source_messages_after_regenerate": len(source_messages),
                "fork_id": forked,
                "workspace_session_ids": ordered_workspace["session_ids"],
                "fork_messages_after_clear": len(fork_messages),
            }
    finally:
        await application.stop()


async def _crash_process(paths: RuntimePaths, workspace: Path) -> None:
    application = await _start(paths, workspace, [])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application.server),
        base_url="http://acceptance",
    ) as client:
        await _request(
            client,
            "POST",
            "/sessions",
            json={
                "session_id": SESSION_ID,
                "thread_id": THREAD_ID,
                "mode": "resume",
            },
        )
    runtime = await application.sessions.get(SESSION_ID, THREAD_ID)
    await runtime.engine.inject(
        PENDING_MARKER,
        source="user",
        message_id="accept-pending",
    )
    snapshot = json.loads(
        paths.session(SESSION_ID).thread(THREAD_ID).inbox_file.read_text(
            encoding="utf-8"
        )
    )
    if [item["message_id"] for item in snapshot["items"]] != ["accept-pending"]:
        raise RuntimeError(f"pending inbox was not persisted: {snapshot}")
    (paths.data_dir.parent / "crash.json").write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "pending_ids": ["accept-pending"],
                "exit": "ungraceful",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    os._exit(0)


async def _recover_process(paths: RuntimePaths, workspace: Path) -> dict[str, Any]:
    application = await _start(
        paths,
        workspace,
        [{"content": "Recovered pending acceptance response."}],
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application.server),
            base_url="http://acceptance",
        ) as client:
            await _request(
                client,
                "POST",
                "/sessions",
                json={
                    "session_id": SESSION_ID,
                    "thread_id": THREAD_ID,
                    "mode": "resume",
                },
            )
            messages: list[dict[str, Any]] = []
            for _ in range(100):
                messages = (
                    await _request(
                        client,
                        "GET",
                        f"/sessions/{SESSION_ID}/threads/{THREAD_ID}/messages",
                    )
                ).json()["messages"]
                if any(
                    message["role"] == "user"
                    and message["content"] == PENDING_MARKER
                    for message in messages
                ) and messages[-1]["role"] == "assistant":
                    break
                await asyncio.sleep(0.01)
            else:
                raise RuntimeError("pending inbox was not resumed after process restart")

            for _ in range(100):
                threads = (
                    await _request(client, "GET", f"/sessions/{SESSION_ID}/threads")
                ).json()["threads"]
                main = next(
                    item for item in threads if item["thread_id"] == THREAD_ID
                )
                if main["turn_status"] == "idle":
                    break
                await asyncio.sleep(0.01)
            else:
                raise RuntimeError("recovered turn did not return to idle")

            failed = await _request(
                client,
                "POST",
                f"/sessions/{SESSION_ID}/threads/{THREAD_ID}/messages",
                json={
                    "request_id": "accept-turn-failure",
                    "content": ERROR_MARKER,
                },
            )
            if "engine_error" not in failed.text:
                raise RuntimeError("controlled provider failure was not surfaced")
            interrupted = await _request(
                client,
                "POST",
                f"/sessions/{SESSION_ID}/threads/{THREAD_ID}/interrupt",
            )
            if interrupted.json()["status"] != "idle":
                raise RuntimeError(f"idle interrupt was not idempotent: {interrupted.text}")
            await _request(client, "POST", f"/sessions/{SESSION_ID}/close")
            return {
                "pid": os.getpid(),
                "pending_recovered": True,
                "messages_after_recovery": len(messages),
                "controlled_failure": True,
            }
    finally:
        await application.stop()


class _BlockingMockLLM(MockLLM):
    def __init__(self) -> None:
        super().__init__([{"content": "unreachable"}])
        self.started = asyncio.Event()

    async def _astream_once(self, messages: list[Any], **kwargs: Any) -> Any:
        self.started.set()
        await asyncio.Event().wait()
        yield self.to_chunk({"content": "unreachable"})


async def _logging_edges_process(
    paths: RuntimePaths,
    workspace: Path,
) -> dict[str, Any]:
    application = await _start(
        paths,
        workspace,
        [
            {
                "tool_calls": [{
                    "id": "denied-read",
                    "name": "read",
                    "args": {"path": "README.md"},
                }]
            },
            {"content": "Denied Tool acceptance response."},
            {
                "tool_calls": [{
                    "id": "failed-goal-update",
                    "name": "update_goal",
                    "args": {
                        "status": "complete",
                        "summary": "There is no active Goal in this session.",
                    },
                }]
            },
            {"content": "Failed Tool acceptance response."},
        ],
    )
    edge_session = "runtime-logging-edges"
    interrupt_session = "runtime-active-interrupt"
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application.server),
            base_url="http://acceptance",
        ) as client:
            await _request(client, "POST", "/sessions", json={
                "session_id": edge_session,
                "thread_id": THREAD_ID,
                "workspace_root": str(workspace),
                "mode": "new",
            })
            await _request(
                client,
                "PATCH",
                f"/sessions/{edge_session}/policy",
                json={"permissions": {"read": "deny", "update_goal": "allow"}},
            )
            denied = await _request(
                client,
                "POST",
                f"/sessions/{edge_session}/threads/{THREAD_ID}/messages",
                json={"request_id": "accept-tool-denied", "content": "deny the read tool"},
            )
            failed = await _request(
                client,
                "POST",
                f"/sessions/{edge_session}/threads/{THREAD_ID}/messages",
                json={"request_id": "accept-tool-failed", "content": "fail a valid Tool operation"},
            )
            if '"type": "end"' not in denied.text or '"type": "end"' not in failed.text:
                raise RuntimeError("Tool edge-case turns did not reach a terminal event")

            blocking = _BlockingMockLLM()
            set_llm_override(application.server, blocking)
            await _request(client, "POST", "/sessions", json={
                "session_id": interrupt_session,
                "thread_id": THREAD_ID,
                "workspace_root": str(workspace),
                "mode": "new",
            })
            turn = asyncio.create_task(client.post(
                f"/sessions/{interrupt_session}/threads/{THREAD_ID}/messages",
                json={"request_id": "accept-active-interrupt", "content": "wait until interrupted"},
            ))
            await asyncio.wait_for(blocking.started.wait(), timeout=2)
            interrupted = await _request(
                client,
                "POST",
                f"/sessions/{interrupt_session}/threads/{THREAD_ID}/interrupt",
            )
            response = await asyncio.wait_for(turn, timeout=2)
            if not interrupted.json()["cancelled"] or '"type": "turn_cancelled"' not in response.text:
                raise RuntimeError("active interrupt did not cancel the running turn")
            await _request(client, "POST", f"/sessions/{edge_session}/close")
            await _request(client, "POST", f"/sessions/{interrupt_session}/close")
            return {
                "pid": os.getpid(),
                "tool_denied": True,
                "tool_failed": True,
                "active_interrupt": True,
            }
    finally:
        await application.stop()


async def _corruption_process(
    paths: RuntimePaths,
    workspace: Path,
) -> dict[str, Any]:
    application = await _start(paths, workspace, [])
    results: dict[str, dict[str, Any]] = {}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=application.server,
                raise_app_exceptions=False,
            ),
            base_url="http://acceptance",
        ) as client:
            for session_id in (
                "corrupt-state",
                "corrupt-jsonl",
                "corrupt-position",
                "corrupt-record",
            ):
                response = await client.post(
                    "/sessions",
                    json={
                        "session_id": session_id,
                        "thread_id": THREAD_ID,
                        "mode": "resume",
                    },
                )
                payload = (
                    response.json()
                    if response.headers.get("content-type", "").startswith(
                        "application/json"
                    )
                    else {}
                )
                error = payload.get("error", payload)
                results[session_id] = {
                    "status": response.status_code,
                    "error_code": error.get("code", ""),
                }
            if any(item["status"] < 400 for item in results.values()):
                raise RuntimeError(f"corrupt persistence resumed: {results}")
            return {"pid": os.getpid(), "cases": results}
    finally:
        await application.stop()


def _prepare_corruption_cases(paths: RuntimePaths) -> None:
    source = paths.session(SESSION_ID).root
    for session_id in (
        "corrupt-state",
        "corrupt-jsonl",
        "corrupt-position",
        "corrupt-record",
    ):
        shutil.copytree(source, paths.session(session_id).root)

    paths.session("corrupt-state").thread(THREAD_ID).plugin_state_file.write_text(
        "{invalid",
        encoding="utf-8",
    )
    jsonl = paths.session("corrupt-jsonl").thread(THREAD_ID).messages_file
    with jsonl.open("ab") as stream:
        stream.write(b'{"schema_version":')

    position_file = paths.session("corrupt-position").thread(THREAD_ID).messages_file
    position_lines = position_file.read_text(encoding="utf-8").splitlines()
    position_record = json.loads(position_lines[0])
    position_record["position"] = 2
    position_lines[0] = json.dumps(position_record, ensure_ascii=False)
    position_file.write_text("\n".join(position_lines) + "\n", encoding="utf-8")

    record_file = paths.session("corrupt-record").thread(THREAD_ID).messages_file
    record_lines = record_file.read_text(encoding="utf-8").splitlines()
    record = json.loads(record_lines[0])
    record["schema_version"] = 2
    record_lines[0] = json.dumps(record, ensure_ascii=False)
    record_file.write_text("\n".join(record_lines) + "\n", encoding="utf-8")


def _physical_evidence(
    paths: RuntimePaths,
    expected_fork_id: str,
) -> dict[str, Any]:
    thread = paths.session(SESSION_ID).thread(THREAD_ID)
    lines = thread.messages_file.read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in lines]
    positions = [record["position"] for record in records]
    if positions != list(range(1, len(records) + 1)):
        raise RuntimeError(f"non-contiguous message positions: {positions}")
    forbidden = {"content", "tool_calls"}
    duplicated = sorted(
        forbidden.intersection(record)
        for record in records
        if forbidden.intersection(record)
    )
    if duplicated:
        raise RuntimeError(f"derived message fields persisted: {duplicated}")
    plugin_state = json.loads(thread.plugin_state_file.read_text(encoding="utf-8"))
    metadata = json.loads(thread.metadata_file.read_text(encoding="utf-8"))
    if metadata["title"] != "Runtime acceptance renamed":
        raise RuntimeError(f"renamed title was not persisted: {metadata['title']!r}")
    process_state = json.loads((paths.data_dir / "state.json").read_text(encoding="utf-8"))
    archived_session_ids = process_state["workspaces.snapshot"]["archived_session_ids"]
    if archived_session_ids != [SESSION_ID]:
        raise RuntimeError(f"archive state was not persisted: {archived_session_ids}")
    workspace_session_ids = process_state["workspaces.snapshot"]["items"][0]["session_ids"]
    if (
        SESSION_ID not in workspace_session_ids
        or expected_fork_id not in workspace_session_ids
        or workspace_session_ids.index(SESSION_ID)
        > workspace_session_ids.index(expected_fork_id)
    ):
        raise RuntimeError(f"session order was not persisted: {workspace_session_ids}")
    inbox = json.loads(thread.inbox_file.read_text(encoding="utf-8"))
    artifact_files = sorted(
        str(path.relative_to(thread.artifacts_dir))
        for path in thread.artifacts_dir.rglob("*")
        if path.is_file()
    )
    return {
        "message_records": len(records),
        "positions": positions,
        "message_record_fields": sorted(records[0]) if records else [],
        "plugin_state_keys": sorted(plugin_state),
        "thread_title": metadata["title"],
        "archived_session_ids": archived_session_ids,
        "workspace_session_ids": workspace_session_ids,
        "inbox": inbox,
        "artifact_files": artifact_files,
    }


def _log_files(log_file: Path) -> list[Path]:
    rotated = sorted(
        log_file.parent.glob(f"{log_file.name}.*"),
        key=lambda path: int(path.suffix[1:]) if path.suffix[1:].isdigit() else 0,
        reverse=True,
    )
    return [*rotated, log_file]


def _log_evidence(log_file: Path) -> dict[str, Any]:
    files = _log_files(log_file)
    text = "".join(path.read_text(encoding="utf-8") for path in files)
    forbidden = [
        USER_MARKER,
        ATTACHMENT_MARKER.decode(),
        API_KEY_MARKER,
        PENDING_MARKER,
        ERROR_MARKER,
    ]
    leaked = [marker for marker in forbidden if marker in text]
    if leaked:
        raise RuntimeError(f"content leaked into runtime log: {leaked}")
    required = [
        "application.boot",
        "application.booted",
        "plugin.state",
        "service.provided",
        "event.dispatch",
        "api.request",
        "api.response",
        "session.opened",
        "session.message.accepted",
        "context.built",
        "llm.request.ready",
        "llm.response",
        "tool.registered",
        "tool.execute.start",
        "tool.execute.finish",
        "persistence.history.appended",
        "persistence.history.loaded",
        "persistence.history.replaced",
        "persistence.inbox.reconciled",
        "persistence.metadata.saved",
        "persistence.lifecycle.appended",
        "persistence.artifact.stored",
        "persistence.artifact.read",
        "state.loaded",
        "state.persisted",
        "config.policy.updated",
        "session.forked",
        "session.renamed",
        "workspace.created",
        "workspace.session.attached",
        "workspace.session.archive.updated",
        "workspace.session.reordered",
        "session.inbox.resuming",
        "session.interrupt",
        "turn.failed",
        "session.closed",
    ]
    missing = [event for event in required if event not in text]
    return {
        "files": [str(path.resolve()) for path in files],
        "bytes": len(text.encode("utf-8")),
        "lines": len(text.splitlines()),
        "required_events": required,
        "missing_events": missing,
        "http_request_correlated": all(
            value in text
            for value in (
                "accept-http-first",
                "accept-turn-first",
                f'session_id="{SESSION_ID}"',
                f'thread_id="{THREAD_ID}"',
            )
        ),
        "content_leaks": leaked,
        "controlled_exception": all(
            value in text
            for value in (
                "turn.failed",
                "Traceback (most recent call last):",
                "RuntimeError",
                "agentloop/engine.py",
            )
        ),
        "tool_outcomes": all(
            value in text
            for value in ('status="success"', 'status="denied"', 'status="error"')
        ),
        "active_interrupt": all(
            value in text
            for value in (
                'request_id="accept-active-interrupt"',
                "session.interrupt",
                "cancelled=true",
            )
        ),
    }


def _logging_policy_evidence(root: Path) -> dict[str, Any]:
    log_file = root / "logging-policy" / "policy.log"
    setup_logging(
        log_file=log_file,
        level="INFO",
        category_levels={"xbotv2.acceptance.debug": "DEBUG"},
    )
    debug_log = RuntimeLog("acceptance.debug")
    info_log = RuntimeLog("acceptance.info")
    debug_log.debug("acceptance.category.debug")
    info_log.debug("acceptance.base.debug.hidden")
    info_log.info("acceptance.base.info")
    info_log.info(
        "acceptance.redaction",
        api_key=API_KEY_MARKER,
        authorization=f"Bearer {API_KEY_MARKER}",
    )
    rotation_log = RuntimeLog("acceptance.rotation")
    for sequence in range(12_000):
        rotation_log.info(
            "acceptance.rotation.record",
            sequence=sequence,
            padding="x" * 480,
        )

    files = _log_files(log_file)
    text = "".join(path.read_text(encoding="utf-8") for path in files)
    evidence = {
        "files": [str(path.resolve()) for path in files],
        "category_debug_present": "acceptance.category.debug" in text,
        "base_debug_absent": "acceptance.base.debug.hidden" not in text,
        "base_info_present": "acceptance.base.info" in text,
        "redacted": API_KEY_MARKER not in text and "<redacted>" in text,
        "rotated": any(path != log_file for path in files),
    }
    evidence["accepted"] = all(
        value for name, value in evidence.items() if name != "files"
    )
    return evidence


def _run_phase(root: Path, phase: str) -> None:
    paths = RuntimePaths.from_data_dir(root.resolve() / "data")
    workspace = root.resolve() / "workspace"
    setup_logging(data_dir=paths.data_dir, level="DEBUG")
    operations = {
        "first": _first_process,
        "second": _second_process,
        "crash": _crash_process,
        "recover": _recover_process,
        "logging_edges": _logging_edges_process,
        "corruption": _corruption_process,
    }
    result = asyncio.run(operations[phase](paths, workspace))
    if phase == "crash":
        raise RuntimeError("crash acceptance phase returned without terminating")
    (root / f"{phase}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _execute_phase(root: Path, phase: str) -> dict[str, Any]:
    completed = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--root",
            str(root.resolve()),
            "--phase",
            phase,
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    (root / f"{phase}.stdout.log").write_text(completed.stdout, encoding="utf-8")
    (root / f"{phase}.stderr.log").write_text(completed.stderr, encoding="utf-8")
    if completed.returncode:
        raise RuntimeError(
            f"acceptance phase {phase} failed with exit {completed.returncode}; "
            f"inspect {root / f'{phase}.stderr.log'}"
        )
    return json.loads((root / f"{phase}.json").read_text(encoding="utf-8"))


def _run(root: Path) -> dict[str, Any]:
    paths, _workspace = _prepare(root)
    first = _execute_phase(root, "first")
    second = _execute_phase(root, "second")
    crashed = _execute_phase(root, "crash")
    inbox_after_crash = json.loads(
        paths.session(SESSION_ID).thread(THREAD_ID).inbox_file.read_text(
            encoding="utf-8"
        )
    )
    recovered = _execute_phase(root, "recover")
    logging_edges = _execute_phase(root, "logging_edges")
    _prepare_corruption_cases(paths)
    corruption = _execute_phase(root, "corruption")
    log_file = paths.logs_dir / "xbotv2.log"
    physical = _physical_evidence(paths, second["fork_id"])
    logs = _log_evidence(log_file)
    logging_policy = _logging_policy_evidence(root)
    process_ids = {
        os.getpid(),
        first["pid"],
        second["pid"],
        crashed["pid"],
        recovered["pid"],
        logging_edges["pid"],
        corruption["pid"],
    }
    process_separated = len(process_ids) == 7
    report = {
        "root": str(root.resolve()),
        "log_file": str(log_file.resolve()),
        "data_dir": str(paths.data_dir.resolve()),
        "first_process": first,
        "second_process": second,
        "crashed_process": crashed,
        "inbox_after_crash": {
            "pending_ids": [
                item["message_id"] for item in inbox_after_crash["items"]
            ]
        },
        "recovery_process": recovered,
        "logging_edges_process": logging_edges,
        "corruption_process": corruption,
        "process_separated": process_separated,
        "physical": physical,
        "logs": logs,
        "logging_policy": logging_policy,
        "accepted": (
            process_separated
            and not logs["missing_events"]
            and logs["http_request_correlated"]
            and not logs["content_leaks"]
            and logs["controlled_exception"]
            and logs["tool_outcomes"]
            and logs["active_interrupt"]
            and logging_policy["accepted"]
        ),
    }
    (root / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


def main() -> None:
    args = _arguments()
    if args.phase:
        _run_phase(args.root, args.phase)
        return
    report = _run(args.root)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["accepted"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
