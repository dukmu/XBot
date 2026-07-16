"""Core execution of plugin-registered subagents."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from xbotv2.api.agents import AgentDefinition
from xbotv2.api.paths import SessionPaths
from xbotv2.api.tools import ToolResult
from xbotv2.core.agents import AgentRegistry


ChildEngineFactory = Callable[[AgentDefinition, str, int], Awaitable[Any]]
TaskCallback = Callable[[dict[str, Any]], Awaitable[None]]
_TERMINAL_STATES = {"completed", "failed", "stopped"}


@dataclass(slots=True)
class SubagentTask:
    id: str
    agent: str
    prompt: str
    thread_id: str
    background: bool
    status: str = "pending"
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0
    output: str = ""
    error: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    runner: asyncio.Task[None] | None = field(default=None, repr=False)

    def snapshot(self, *, full_output: bool = False) -> dict[str, Any]:
        limit = len(self.output) if full_output else 2_000
        output = self.output if full_output else _preview(self.output, limit)
        return {
            "task_id": self.id,
            "kind": "agent",
            "command": f"{self.agent}: {_preview(self.prompt, 1_000)}",
            "cwd": "",
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "output": output,
            "error": _preview(self.error, 2_000),
            "agent": self.agent,
            "thread_id": self.thread_id,
            "usage": dict(self.usage),
        }


class SubagentManager:
    """Run child Engines in thread-local state under the parent session."""

    def __init__(
        self,
        *,
        registry: AgentRegistry,
        session_paths: SessionPaths,
        parent_thread_id: str,
        engine_factory: ChildEngineFactory,
        depth: int = 0,
        max_depth: int = 3,
        max_concurrency: int = 4,
    ) -> None:
        self.registry = registry
        self.session_paths = session_paths
        self.parent_thread_id = parent_thread_id
        self.engine_factory = engine_factory
        self.depth = depth
        self.max_depth = max_depth
        self.max_concurrency = max_concurrency
        self.on_update: TaskCallback | None = None
        self.on_complete: TaskCallback | None = None
        self._tasks: dict[str, SubagentTask] = {}
        self._next_id = 1
        self._closing = False

    async def run(
        self,
        agent: str,
        prompt: str,
        background: bool = False,
    ) -> ToolResult:
        """Run one registered subagent, optionally returning immediately."""
        definition = self.registry.get(agent)
        if definition is None or definition.mode == "primary":
            return ToolResult.failure("agent_not_found", f"Unknown subagent: {agent}")
        if not prompt.strip():
            return ToolResult.failure("invalid_prompt", "Subagent prompt cannot be empty")
        if self.depth >= self.max_depth:
            return ToolResult.failure(
                "subagent_depth_exceeded",
                f"Subagent nesting is limited to {self.max_depth} levels",
            )
        if self._closing:
            return ToolResult.failure("session_closing", "Session is closing")
        if background and self.on_complete is None:
            return ToolResult.failure(
                "background_unavailable",
                "Background subagents require a live session mailbox",
            )
        if self._active_count() >= self.max_concurrency:
            return ToolResult.failure(
                "subagent_limit_reached",
                f"At most {self.max_concurrency} subagents may run concurrently",
            )

        task = SubagentTask(
            id=f"agent-task-{self._next_id}",
            agent=definition.name,
            prompt=prompt,
            thread_id=self._new_thread_id(definition.name),
            background=background,
        )
        self._next_id += 1
        self._tasks[task.id] = task
        self._record_thread("started", task)
        await self._notify(task)

        if background:
            task.runner = asyncio.create_task(
                self._run_background(task, definition),
                name=f"xbotv2-{task.id}",
            )
            return ToolResult.success(
                f"Started {task.id} in thread {task.thread_id}",
                data=task.snapshot(),
            )
        return await self._execute(task, definition)

    async def list_tasks(self, task_id: str | None = None) -> ToolResult:
        """List subagent tasks or retrieve one task's complete final output."""
        if task_id:
            task = self._tasks.get(task_id)
            if task is None:
                return ToolResult.failure("task_not_found", f"Unknown task: {task_id}")
            data: Any = task.snapshot(full_output=True)
        else:
            data = [task.snapshot() for task in self._tasks.values()]
        return ToolResult.success(json.dumps(data, ensure_ascii=False), data=data)

    async def stop_task(self, task_id: str) -> ToolResult:
        """Stop one background subagent task by its stable task ID."""
        task = self._tasks.get(task_id)
        if task is None:
            return ToolResult.failure("task_not_found", f"Unknown task: {task_id}")
        if task.status in _TERMINAL_STATES:
            return ToolResult.success(
                f"{task_id} is already {task.status}", data=task.snapshot()
            )
        if task.runner is None:
            return ToolResult.failure(
                "task_not_background",
                f"{task_id} is attached to the current blocking turn",
            )
        task.runner.cancel()
        await asyncio.gather(task.runner, return_exceptions=True)
        return ToolResult.success(f"Stopped {task_id}", data=task.snapshot())

    async def stop_all(self) -> list[dict[str, Any]]:
        active = [
            task
            for task in self._tasks.values()
            if task.runner is not None and task.status not in _TERMINAL_STATES
        ]
        for task in active:
            task.runner.cancel()
        if active:
            await asyncio.gather(
                *(task.runner for task in active if task.runner is not None),
                return_exceptions=True,
            )
        return [task.snapshot() for task in active]

    def snapshots(self) -> list[dict[str, Any]]:
        return [task.snapshot() for task in self._tasks.values()]

    def definitions(self) -> tuple[AgentDefinition, ...]:
        return self.registry.definitions()

    async def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        await self.stop_all()
        self.on_update = None
        self.on_complete = None

    async def _run_background(
        self,
        task: SubagentTask,
        definition: AgentDefinition,
    ) -> None:
        await self._execute(task, definition)
        if not self._closing and self.on_complete is not None:
            await self.on_complete(task.snapshot(full_output=True))

    async def _execute(
        self,
        task: SubagentTask,
        definition: AgentDefinition,
    ) -> ToolResult:
        task.status = "running"
        task.started_at = time.time()
        await self._notify(task)
        child = None
        try:
            child = await self.engine_factory(
                definition, task.thread_id, self.depth + 1
            )
            await child.start_session()
            async for event in child.run_turn(task.prompt):
                event_type = event.get("type")
                data = event.get("data") or {}
                if event_type == "assistant_message":
                    task.output = str(data.get("content") or "")
                elif event_type == "error":
                    task.error = str(data.get("message") or "Subagent turn failed")
                elif event_type == "turn_cancelled":
                    task.error = str(
                        data.get("reason") or "Subagent turn was cancelled"
                    )
            task.usage = dict(getattr(child, "session_usage", {}) or {})
            close_error = await self._close_child(child)
            child = None
            if close_error:
                task.error = close_error
            if task.error:
                task.status = "failed"
            elif task.output:
                task.status = "completed"
            else:
                task.status = "failed"
                task.error = "Subagent completed without an assistant response"
        except asyncio.CancelledError:
            if child is not None:
                with suppress(BaseException):
                    await asyncio.shield(child.close_session())
            task.status = "stopped"
            self._record_thread("cancelled", task)
            raise
        except Exception as exc:
            if child is not None:
                close_error = await self._close_child(child)
                if close_error:
                    exc.add_note(f"Child close also failed: {close_error}")
            task.status = "failed"
            task.error = str(exc) or type(exc).__name__
        finally:
            task.finished_at = time.time()
            if task.status != "stopped":
                self._record_thread(task.status, task, error=task.error)
            await self._notify(task)

        if task.status == "completed":
            return ToolResult.success(task.output, data=task.snapshot())
        return ToolResult.failure("subagent_failed", task.error)

    def _active_count(self) -> int:
        return sum(
            task.status not in _TERMINAL_STATES for task in self._tasks.values()
        )

    def _new_thread_id(self, agent: str) -> str:
        while True:
            thread_id = f"{agent}-{secrets.token_hex(3)}"
            if not self.session_paths.has_thread(thread_id):
                return thread_id

    async def _close_child(self, child: Any) -> str:
        try:
            await child.close_session()
        except Exception as exc:
            return f"Subagent close failed: {exc}"
        return ""

    async def _notify(self, task: SubagentTask) -> None:
        if self.on_update is not None:
            await self.on_update(task.snapshot())

    def _record_thread(
        self,
        event: str,
        task: SubagentTask,
        *,
        error: str = "",
    ) -> None:
        path = self.session_paths.threads_log
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "event": event,
            "thread_id": task.thread_id,
            "parent_thread_id": self.parent_thread_id,
            "agent": task.agent,
            "task_id": task.id,
            "background": task.background,
            "depth": self.depth + 1,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if error:
            record["error"] = error
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())


def _preview(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit]}\n[truncated; {len(value) - limit} characters omitted]"


__all__ = ["SubagentManager"]
