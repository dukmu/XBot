"""Configuration and HTTP request models."""
from pydantic import BaseModel, ConfigDict, Field


class TeamConfig(BaseModel):
    enabled: bool = False
    model_config = ConfigDict(extra="forbid")


class PublishRequest(BaseModel):
    request_id: str = Field(min_length=1)
    content: str = Field(min_length=1)
    mentions: tuple[str, ...] = ()
    model_config = ConfigDict(extra="forbid")
