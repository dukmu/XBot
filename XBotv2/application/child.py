"""Lifecycle adapter for one child Agent application."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from XBotv2.application.host import mounted_application
from XBotv2.application.contracts import AgentApplicationPort, ChildApplicationRequest
from XBotv2.application import ChildApplicationError, ChildApplicationResult
from XBotv2.agentloop import HumanInput, InboxItem, InboxTarget
from XBotv2.persistence import ThreadLifecycleRecord
from XBotv2.persistence import ThreadLifecycleWriterPort
from XBotv2.core.paths import RuntimePaths
from XBotv2.core.providers import BaseProvider
from XBotv2.agentloop.protocol import (
    AssistantCompleted,
    LoopError,
    LoopTurnEnded,
    TurnCancelled,
)
from XBotv2.core.parts import TextPart
from XBotv2.core.domain import ModelRoute


@dataclass(slots=True)
class ChildApplications:
    """Create child Agent applications from one bound parent application."""

    paths: RuntimePaths
    provider_name: str | None
    session_id: str
    workspace_root: Path
    no_plugins: bool
    plugin_dirs: list[Path | str] | None
    llm_override: BaseProvider | None
    parent_thread_id: str
    interactive: bool
    async def spawn(
        self,
        request: ChildApplicationRequest,
        lifecycle: ThreadLifecycleWriterPort,
    ) -> "ChildApplicationSession":
        from XBotv2.application.app import start_application

        route = request.definition.model_policy.route
        child_ctx = await start_application(
            paths=self.paths,
            provider_name=(
                route.provider if isinstance(route, ModelRoute) else self.provider_name
            ),
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
            application=mounted_application(child_ctx),
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
    """Run and release a child application through its public handle."""

    application: AgentApplicationPort
    prompt: str
    agent: str
    thread_id: str
    parent_thread_id: str
    lifecycle: ThreadLifecycleWriterPort

    def record_started(self) -> None:
        self._record("started")

    async def wait(self) -> ChildApplicationResult:
        engine = self.application.driver
        output = ""
        error = ""
        try:
            await engine.start_session()
            async for event in engine.run_turn(InboxItem(
                target=InboxTarget.NEXT_TURN,
                input=HumanInput(content=self.prompt),
            )):
                if isinstance(event, AssistantCompleted):
                    output = "".join(
                        part.text
                        for part in event.message.parts
                        if isinstance(part, TextPart)
                    )
                elif isinstance(event, LoopError):
                    error = event.message or "Subagent turn failed"
                elif isinstance(event, LoopTurnEnded) and isinstance(
                    event.outcome, TurnCancelled
                ):
                    error = event.outcome.reason
        except asyncio.CancelledError:
            with suppress(BaseException):
                await asyncio.shield(self._close())
            self._record("cancelled", error=error)
            raise
        except BaseException as exc:
            # A failed child turn still owns a mounted application and the
            # session lock. Close it before reporting the failure so a
            # provider/tool exception cannot leave a zombie subagent behind.
            failure = str(exc) or type(exc).__name__
            try:
                close_error = await self._close()
            except BaseException as close_exc:  # noqa: BLE001 — close is best effort
                close_error = f"Subagent close failed: {close_exc}"
            if close_error:
                failure = f"{failure}; {close_error}"
            self._record("failed", error=failure)
            raise ChildApplicationError(failure) from exc

        usage = self.application.usage.snapshot()
        try:
            close_error = await self._close()
        except BaseException as close_exc:  # noqa: BLE001 — close is best effort
            close_error = f"Subagent close failed: {close_exc}"
        if close_error and not error:
            error = close_error
        if error:
            self._record("failed", error=error)
            raise ChildApplicationError(error)
        if not output:
            error = "Subagent completed without an assistant response"
            self._record("failed", error=error)
            raise ChildApplicationError(error)
        self._record("completed")
        return ChildApplicationResult(final_response=output, usage=usage)

    async def cancel(self) -> None:
        """The owning job cancels ``wait``; its cancellation path closes us."""

    async def _close(self) -> str:
        try:
            await self.application.driver.close_session()
        except Exception as exc:  # noqa: BLE001 - close errors become results
            return f"Subagent close failed: {exc}"
        finally:
            await self.application.close()
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
