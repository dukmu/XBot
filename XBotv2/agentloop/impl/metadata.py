"""Thread-local metadata lifecycle owned by the loop runtime."""

from __future__ import annotations
from xcore.api.context import Context
from XBotv2.core.api.domain import ResolvedRuntimeSelection
from XBotv2.core.api.metadata import (
    ThreadMetadata,
    ThreadMetadataLifecycle,
    MetadataReady,
    MetadataUninitialized,
    ThreadMetadataNotInitialized,
    ThreadMetadataChanged,
    ThreadMetadataInitialized,
    THREAD_METADATA_CHANGED,
    THREAD_METADATA_INITIALIZED,
)


class ThreadMetadataState:
    """Own one thread's metadata lifecycle and publish each state transition.

    A runtime starts without effective metadata. The Agent resolver or
    persistence hydration initializes it exactly once; subsequent replacements
    publish the complete previous and current values.
    """

    name = "thread_metadata"

    def __init__(
        self,
        ctx: Context,
        *,
        session_id: str,
        thread_id: str,
    ) -> None:
        self.ctx = ctx
        self._session_id = session_id
        self._thread_id = thread_id
        self._state: ThreadMetadataLifecycle = MetadataUninitialized()

    @property
    def value(self) -> ThreadMetadata:
        if isinstance(self._state, MetadataUninitialized):
            raise ThreadMetadataNotInitialized(
                f"Thread metadata is not initialized for "
                f"{self._session_id}/{self._thread_id}"
            )
        return self._state.metadata

    @property
    def lifecycle(self) -> ThreadMetadataLifecycle:
        return self._state

    async def initialize(self, value: ThreadMetadata) -> None:
        if not isinstance(self._state, MetadataUninitialized):
            raise RuntimeError(
                f"Thread metadata already initialized for "
                f"{self._session_id}/{self._thread_id}"
            )
        value = value.with_default_title(
            session_id=self._session_id,
            thread_id=self._thread_id,
        )
        self._state = MetadataReady(value)
        await self.ctx.emit(
            THREAD_METADATA_INITIALIZED,
            ThreadMetadataInitialized(
                session_id=self._session_id,
                thread_id=self._thread_id,
                current=value,
            ),
        )

    async def replace(self, value: ThreadMetadata) -> None:
        value = value.with_default_title(
            session_id=self._session_id,
            thread_id=self._thread_id,
        )
        previous = self.value
        if value == previous:
            return
        self._state = MetadataReady(value)
        await self.ctx.emit(
            THREAD_METADATA_CHANGED,
            ThreadMetadataChanged(
                session_id=self._session_id,
                thread_id=self._thread_id,
                previous=previous,
                current=value,
            ),
        )

    async def replace_runtime_selection(
        self,
        selection: ResolvedRuntimeSelection,
    ) -> None:
        await self.replace(
            self.value.model_copy(update={"runtime_selection": selection})
        )
