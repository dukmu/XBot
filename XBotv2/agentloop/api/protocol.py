"""Tool catalog transport values shared by server and client."""
from pydantic import Field, JsonValue
from XBotv2.protocol import WireModel

class ToolInfo(WireModel):
    name: str = Field(min_length=1)
    registered_name: str = Field(min_length=1)
    namespace: str = Field(min_length=1)
    description: str
    parameters: dict[str, JsonValue]
    timeout_seconds: float | None = Field(default=None, gt=0)


class ToolListResponse(WireModel):
    tools: list[ToolInfo] = Field(default_factory=list)
