"""Canonical usage capability and its feature-owned event."""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from XBotv2.core.domain import RequestObservation, UsageDelta, UsageSnapshot


USAGE_STATE_NAMESPACE = "usage"
USAGE_SNAPSHOT_KEY = "snapshot"


class UsageUpdated(BaseModel):
    kind: Literal["usage_updated"] = "usage_updated"
    snapshot: UsageSnapshot
    model_config = ConfigDict(extra="forbid", frozen=True)


class UsagePort(Protocol):
    def snapshot(self) -> UsageSnapshot: ...

    async def record(
        self,
        observation: RequestObservation,
        usage: UsageDelta,
    ) -> UsageSnapshot: ...


__all__ = [
    "USAGE_SNAPSHOT_KEY",
    "USAGE_STATE_NAMESPACE",
    "UsagePort",
    "UsageUpdated",
]
