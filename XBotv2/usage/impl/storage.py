"""Usage owns the interpretation of its durable fixed-size counters."""

from XBotv2.core.api.domain import TokenCounters, UsageSnapshot
from XBotv2.core.api.messages import AssistantMessage
from XBotv2.usage.api.service import USAGE_STATE_NAMESPACE, USAGE_COUNTERS_KEY


class UsageArchive:
    def __init__(self, backend, archive):
        self.backend = backend
        self.archive = archive

    async def read(self, session_id, thread_id):
        stored = (
            await self.backend.state(session_id, thread_id)
            .namespace(USAGE_STATE_NAMESPACE)
            .get(USAGE_COUNTERS_KEY)
        )
        if stored is None:
            return UsageSnapshot()
        if not isinstance(stored, dict) or set(stored) != {"total_counters"}:
            raise ValueError("Persisted usage must contain only total_counters")
        counters = TokenCounters.model_validate(stored["total_counters"])
        latest = next(
            (
                m.exchange.observation
                for m in reversed(await self.archive.surface(session_id, thread_id))
                if isinstance(m, AssistantMessage)
                and m.exchange.observation.purpose.kind == "turn"
            ),
            None,
        )
        return UsageSnapshot(total_counters=counters, latest_turn_observation=latest)
