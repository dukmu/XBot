"""ACP carrier configuration and foreground protocol lifetime."""
from typing import Protocol
from pydantic import BaseModel, ConfigDict
from xcore.api.services import ServiceKey

class ACPConfig(BaseModel):
    provider_name: str | None = None
    no_plugins: bool = False
    selected_agent: str | None = None
    model_config = ConfigDict(extra="forbid", frozen=True)

class ACPServer(Protocol):
    async def serve(self) -> None: ...
    async def close(self) -> None: ...

ACP_SERVER: ServiceKey[ACPServer] = ServiceKey("acp.server")
