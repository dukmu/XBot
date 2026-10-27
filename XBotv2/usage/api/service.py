"""Usage accounting capability and persisted state keys."""
from typing import Protocol
from xcore.api.services import ServiceKey
from XBotv2.core.api.domain import RequestObservation, UsageDelta, UsageSnapshot

USAGE_STATE_NAMESPACE = "usage"
USAGE_COUNTERS_KEY = "counters"

class UsagePort(Protocol):
    def snapshot(self) -> UsageSnapshot: ...
    async def record(self, observation: RequestObservation, usage: UsageDelta) -> UsageSnapshot: ...

USAGE = ServiceKey[UsagePort]("usage")
