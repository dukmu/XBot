"""Thread status contributions shown by connected clients."""
from dataclasses import dataclass, field
from xcore.api.context import Context

COLLECT_STATUS_SLOTS = "interactions/status-slots/collect"

@dataclass(slots=True)
class StatusSlots:
    values: dict[str, str] = field(default_factory=dict)
    def add(self, name: str, value: str) -> None:
        name, value = str(name).strip(), str(value).strip()
        if name and value:
            self.values[name] = value

async def status_slots(ctx: Context) -> dict[str, str]:
    slots = StatusSlots()
    await ctx.emit(COLLECT_STATUS_SLOTS, slots)
    return dict(slots.values)
