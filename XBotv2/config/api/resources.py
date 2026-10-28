"""Owner-contributed configuration documents exposed to clients."""
from __future__ import annotations

from typing import Literal, Protocol
from pydantic import BaseModel, ConfigDict, JsonValue
from xcore.api.services import ServiceKey
from XBotv2.config.api.contracts import PluginConfigScope


class ConfigurationResourceNotFound(LookupError):
    pass


class ConfigurationResource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    title: str
    scopes: tuple[PluginConfigScope, ...]
    format: Literal["json", "yaml", "markdown"]
    description: str = ""


class ConfigurationCatalog(BaseModel):
    resources: tuple[ConfigurationResource, ...]


class ConfigurationDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    scope: PluginConfigScope
    revision: str
    content: JsonValue
    config_schema: dict[str, JsonValue] | None = None


class ConfigurationProvider(Protocol):
    async def catalog(self, thread_ctx) -> tuple[ConfigurationResource, ...]: ...
    async def read(self, thread_ctx, resource_id: str, scope: PluginConfigScope) -> ConfigurationDocument: ...
    async def save(self, thread_ctx, document: ConfigurationDocument) -> ConfigurationDocument: ...


class ConfigurationResourcesPort(Protocol):
    def register(self, namespace: str, provider: ConfigurationProvider) -> None: ...
    async def catalog(self, thread_ctx) -> ConfigurationCatalog: ...
    async def read(self, thread_ctx, resource_id: str, scope: PluginConfigScope) -> ConfigurationDocument: ...
    async def save(self, thread_ctx, document: ConfigurationDocument) -> ConfigurationDocument: ...


CONFIG_RESOURCES: ServiceKey[ConfigurationResourcesPort] = ServiceKey("config.resources")
