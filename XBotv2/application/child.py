"""Lifecycle adapter for one child Agent application."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from XBotv2.core.agents import AgentSessionResult, SubagentTurnError
from XBotv2.core.paths import SessionPaths


@dataclass(slots=True)
class ChildApplications:
    """Create child Agent applications from one bound parent application."""

    paths: Any
    provider_name: str
    session_id: str
    workspace_root: Any
    plugin_dirs: list[Any] | None
    llm_override: Any
    parent_thread_id: str
    interactive: bool
    session_paths: SessionPaths
    _parent: Any = None

    def bind(self, parent: Any) -> None:
        self._parent = parent

    async def __call__(
        self,
        definition: Any,
        child_thread_id: str,
        prompt: str,
    ) -> "ChildApplicationSession":
        if self._parent is None:
            raise RuntimeError("child application factory is not bound")
        from XBotv2.application.app import start_application

        child_ctx = await start_application(
            paths=self.paths,
            provider_name=definition.provider or self.provider_name,
            session_id=self.session_id,
            thread_id=child_thread_id,
            workspace_root=self.workspace_root,
            plugin_dirs=self.plugin_dirs,
            llm_override=self.llm_override,
            agent_definition=definition,
            parent_permission_system=self._parent.get(
                "permissions", strict=False
            ),
            parent_thread_id=self.parent_thread_id,
            is_subagent=True,
            interactive=self.interactive,
            client_events=(
                self._parent.client_events if self.interactive else None
            ),
        )
        child = ChildApplicationSession(
            context=child_ctx,
            prompt=prompt,
            agent=definition.name,
            thread_id=child_thread_id,
            session_paths=self.session_paths,
            parent_thread_id=self.parent_thread_id,
        )
        child.record_started()
        return child


@dataclass(slots=True)
class ChildApplicationSession:
    """Run and release a child application through the AgentSession contract."""

    context: Any
    prompt: str
    agent: str
    thread_id: str
    session_paths: SessionPaths
    parent_thread_id: str

    def record_started(self) -> None:
        self._record("started")

    async def wait(self) -> AgentSessionResult:
        engine = self.context.engine
        await engine.start_session()
        output = ""
        error = ""
        try:
            async for event in engine.run_turn(self.prompt):
                event_type = event.get("type")
                data = event.get("data") or {}
                if event_type == "assistant_message":
                    output = str(data.get("content") or "")
                elif event_type == "error":
                    error = str(data.get("message") or "Subagent turn failed")
                elif event_type == "turn_cancelled":
                    error = str(
                        data.get("reason") or "Subagent turn was cancelled"
                    )
        except asyncio.CancelledError:
            with suppress(BaseException):
                await asyncio.shield(self._close())
            self._record("cancelled", error=error)
            raise

        usage_service = self.context.get("usage", strict=False)
        usage = usage_service.snapshot() if usage_service is not None else {}
        close_error = await self._close()
        if close_error and not error:
            error = close_error
        if error:
            self._record("failed", error=error)
            raise SubagentTurnError(error)
        if not output:
            error = "Subagent completed without an assistant response"
            self._record("failed", error=error)
            raise SubagentTurnError(error)
        self._record("completed")
        return AgentSessionResult(final_response=output, usage=usage)

    async def cancel(self) -> None:
        """The owning job cancels ``wait``; its cancellation path closes us."""

    async def _close(self) -> str:
        try:
            await self.context.engine.close_session()
        except Exception as exc:  # noqa: BLE001 - close errors become results
            return f"Subagent close failed: {exc}"
        finally:
            await self.context.stop()
        return ""

    def _record(self, event: str, *, error: str = "") -> None:
        path = self.session_paths.threads_log
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "event": event,
            "thread_id": self.thread_id,
            "parent_thread_id": self.parent_thread_id,
            "agent": self.agent,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if error:
            record["error"] = error
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())


__all__ = ["ChildApplicationSession", "ChildApplications"]
