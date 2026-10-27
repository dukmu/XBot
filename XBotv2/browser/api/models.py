"""Configuration contract owned by the browser plugin."""

from pydantic import BaseModel, ConfigDict, Field


class SearchPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backend: str = "yandex"
    region: str = "wt-wt"
    safesearch: str = "moderate"


class NetworkPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timeout: float = Field(default=20.0, gt=0)
    max_bytes: int = Field(default=5_000_000, ge=1)
    private_access: bool = False


class BrowserSessionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    headless: bool = True
    timeout: float = Field(default=30.0, gt=0)


class BrowserConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    search: SearchPolicy = Field(default_factory=SearchPolicy)
    network: NetworkPolicy = Field(default_factory=NetworkPolicy)
    session: BrowserSessionPolicy = Field(default_factory=BrowserSessionPolicy)


__all__ = [
    "BrowserConfig",
    "BrowserSessionPolicy",
    "NetworkPolicy",
    "SearchPolicy",
]
