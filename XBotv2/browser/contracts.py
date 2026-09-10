"""Configuration contract owned by the browser plugin."""

from pydantic import BaseModel, ConfigDict, Field


class BrowserSearchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backend: str = "yandex"
    region: str = "wt-wt"
    safesearch: str = "moderate"


class BrowserNetworkConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timeout_seconds: float = Field(default=20.0, gt=0)
    max_response_bytes: int = Field(default=5_000_000, ge=1)
    allow_private: bool = False


class BrowserSessionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    headless: bool = True
    timeout_seconds: float = Field(default=30.0, gt=0)


class BrowserConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    search: BrowserSearchConfig = Field(default_factory=BrowserSearchConfig)
    network: BrowserNetworkConfig = Field(default_factory=BrowserNetworkConfig)
    browser: BrowserSessionConfig = Field(default_factory=BrowserSessionConfig)


__all__ = [
    "BrowserConfig",
    "BrowserNetworkConfig",
    "BrowserSearchConfig",
    "BrowserSessionConfig",
]
