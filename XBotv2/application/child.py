"""Lifecycle adapter for one child Agent application."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Literal

from XBotv2.application.services import ChildApplicationRequest
from XBotv2.agents import AgentSessionResult, SubagentTurnError
from XBotv2.persistence.models import ThreadLifecycleRecord
from XBotv2.persistence import ThreadLifecycleWriterPort


@dataclass(slots=True)
class ChildApplications:
    """Create child Agent applications from one bound parent application."""

    paths: Any
    provider_name: str
    session_id: str
    workspace_root: Any
    no_plugins: bool
    plugin_dirs: list[Any] | None
    llm_override: Any
    parent_thread_id: str
    interactive: bool
    async def spawn(
        self,
        request: ChildApplicationRequest,
        lifecycle: ThreadLifecycleWriterPort,
    ) -> "ChildApplicationSession":
        from XBotv2.application.app import start_application

        child_ctx = await start_application(
            paths=self.paths,
            provider_name=request.definition.provider or self.provider_name,
            session_id=self.session_id,
            thread_id=request.thread_id,
            workspace_root=self.workspace_root,
            no_plugins=self.no_plugins,
            plugin_dirs=self.plugin_dirs,
            llm_override=self.llm_override,
            agent_definition=request.definition,
            parent_permission_system=request.parent_permissions,
            parent_thread_id=self.parent_thread_id,
            is_subagent=True,
            interactive=self.interactive,
            client_events=request.client_events if self.interactive else None,
        )
        child = ChildApplicationSession(
            context=child_ctx,
            prompt=request.prompt,
            agent=request.definition.name,
            thread_id=request.thread_id,
            parent_thread_id=self.parent_thread_id,
            lifecycle=lifecycle,
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
    parent_thread_id: str
    lifecycle: ThreadLifecycleWriterPort

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
            await self.context.destroy()
        return ""

    def _record(
        self,
        event: Literal["started", "completed", "failed", "cancelled"],
        *,
        error: str = "",
    ) -> None:
        self.lifecycle.append(
            ThreadLifecycleRecord.create(
                event,
                thread_id=self.thread_id,
                parent_thread_id=self.parent_thread_id,
                agent=self.agent,
                error=error,
            )
        )


__all__ = ["ChildApplicationSession", "ChildApplications"]
